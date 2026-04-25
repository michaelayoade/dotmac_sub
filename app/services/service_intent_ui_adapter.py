"""UI-facing service intent adapter.

Keeps admin/customer web services from importing network intent helpers
directly. Network modules still own the actual ONT interpretation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry


class ServiceIntentUiAdapter:
    name = "service_intent.ui"

    def build_ont_service_intent(
        self,
        ont: object,
        *,
        db: Session | None = None,
        subscriber_info: dict[str, object] | None = None,
        ont_plan: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        from app.services.network.ont_service_intent import build_service_intent

        return build_service_intent(
            ont,
            db=db,
            subscriber_info=subscriber_info,
            ont_plan=ont_plan,
        )

    def load_ont_plan_for_ont(self, db: Session, *, ont_id: str) -> dict[str, Any]:
        from app.services.network.ont_service_intent import load_ont_plan_for_ont

        return load_ont_plan_for_ont(db, ont_id=ont_id)

    def load_acs_observed_service_intent(
        self,
        db: Session,
        *,
        ont_id: str,
    ) -> dict[str, object]:
        """Return ACS/TR-069 observed state mapped to service intent UI data."""
        from app.services.acs_service_intent_adapter import acs_service_intent_adapter

        return acs_service_intent_adapter.load_observed_intent_for_ont(db, ont_id=ont_id)

    def build_acs_observed_service_intent(
        self,
        summary: object | None,
    ) -> dict[str, object]:
        """Map an existing ACS/TR-069 summary without another ACS fetch."""
        from app.services.acs_service_intent_adapter import acs_service_intent_adapter

        return acs_service_intent_adapter.build_observed_intent(summary)

    def resolve_effective_tr069_profile(
        self, db: Session, *, ont: object
    ) -> tuple[object | None, str | None]:
        """Resolve the TR-069 OLT profile selected for an ONT UI."""
        from app.services import web_network_onts as web_network_onts_service

        return web_network_onts_service.resolve_effective_tr069_profile_for_ont(db, ont)

    def ont_capabilities(self, db: Session, *, ont_id: str) -> dict[str, object]:
        """Return ONT feature capabilities through the UI adapter boundary."""
        from app.services.network.ont_read import OntReadFacade

        return OntReadFacade.get_capabilities(db, ont_id)

    def profile_service_port_defaults(
        self,
        ont: object,
        *,
        db: Session | None = None,
        service_ports: list[object] | None = None,
    ) -> dict[str, object]:
        """Return OLT config pack + ONT desired-config service-port defaults."""
        config_pack = None
        effective_values: dict[str, object] = {}
        if db is not None:
            from app.services.network.effective_ont_config import (
                resolve_effective_ont_config,
            )

            effective = resolve_effective_ont_config(db, ont)
            effective_values = (
                effective.get("values", {}) if isinstance(effective, dict) else {}
            )
            config_pack = effective.get("config_pack") if isinstance(effective, dict) else None

        vlan_id = effective_values.get("wan_vlan")
        gem_index = effective_values.get("wan_gem_index") or 1
        gem_choices = [int(gem_index)]
        if config_pack:
            gem_choices = sorted(
                {
                    int(config_pack.internet_gem_index),
                    int(config_pack.mgmt_gem_index),
                    int(config_pack.voip_gem_index),
                    int(config_pack.iptv_gem_index),
                }
            )

        actual_vlans = {
            getattr(port, "vlan_id", None)
            for port in (service_ports or [])
            if getattr(port, "vlan_id", None) is not None
        }
        planned_vlans = {vlan_id} if vlan_id is not None else set()
        missing_vlans = sorted(planned_vlans - actual_vlans)
        extra_vlans = sorted(actual_vlans - planned_vlans) if planned_vlans else []

        return {
            "primary_vlan_id": vlan_id,
            "primary_gem_index": int(gem_index or 1),
            "primary_user_vlan": None,
            "primary_tag_transform": "default",
            "gem_choices": gem_choices,
            "user_vlan_choices": [],
            "config_pack_source": config_pack.olt_name if config_pack else None,
            "planned_services": [
                {
                    "name": "Internet",
                    "service_type": "internet",
                    "s_vlan": vlan_id,
                    "c_vlan": None,
                    "gem_port_id": int(gem_index or 1),
                    "connection_type": effective_values.get("wan_mode"),
                }
            ]
            if vlan_id is not None
            else [],
            "missing_vlans": missing_vlans,
            "extra_vlans": extra_vlans,
        }

service_intent_ui_adapter = ServiceIntentUiAdapter()
adapter_registry.register(service_intent_ui_adapter)
