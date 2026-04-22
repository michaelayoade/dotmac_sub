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

    def apply_bundle_to_ont(
        self,
        db: Session,
        *,
        ont_id: str,
        bundle_id: str,
        create_wan_instances: bool = True,
        push_to_device: bool = False,
    ) -> object:
        """Apply a provisioning bundle through the network-owned bundle adapter."""
        from app.services.network.ont_profile_apply import apply_bundle_to_ont

        return apply_bundle_to_ont(
            db,
            ont_id,
            bundle_id,
            create_wan_instances=create_wan_instances,
            push_to_device=push_to_device,
        )

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
        """Return profile-derived service-port defaults for operator UI forms."""
        profile = None
        if db is not None:
            from app.services.network.ont_bundle_assignments import resolve_assigned_bundle

            profile = resolve_assigned_bundle(db, ont)
        if profile is None:
            profile = getattr(ont, "provisioning_profile", None)
        services = list(
            getattr(profile, "wan_services", None) or []
        )
        services = [svc for svc in services if getattr(svc, "is_active", False)]
        primary = services[0] if services else None
        gem_choices = sorted(
            {
                int(gem)
                for gem in (getattr(service, "gem_port_id", None) for service in services)
                if gem is not None
            }
        )
        user_vlan_choices = sorted(
            {
                int(c_vlan)
                for c_vlan in (getattr(service, "c_vlan", None) for service in services)
                if c_vlan is not None
            }
        )

        vlan_id = getattr(primary, "s_vlan", None) if primary else None
        gem_index = getattr(primary, "gem_port_id", None) if primary else None
        c_vlan = getattr(primary, "c_vlan", None) if primary else None
        vlan_mode = getattr(primary, "vlan_mode", None) if primary else None
        vlan_mode_value = getattr(vlan_mode, "value", vlan_mode)
        tag_transform = (
            "translate"
            if c_vlan
            else "transparent"
            if vlan_mode_value == "transparent"
            else "default"
        )

        actual_vlans = {
            getattr(port, "vlan_id", None)
            for port in (service_ports or [])
            if getattr(port, "vlan_id", None) is not None
        }
        planned_vlans = {
            getattr(service, "s_vlan", None)
            for service in services
            if getattr(service, "s_vlan", None) is not None
        }
        missing_vlans = sorted(planned_vlans - actual_vlans)
        extra_vlans = sorted(actual_vlans - planned_vlans) if planned_vlans else []

        return {
            "primary_vlan_id": vlan_id,
            "primary_gem_index": gem_index or 1,
            "primary_user_vlan": c_vlan,
            "primary_tag_transform": tag_transform,
            "gem_choices": gem_choices or ([gem_index] if gem_index is not None else [1]),
            "user_vlan_choices": user_vlan_choices,
            "planned_services": [
                {
                    "name": getattr(service, "name", None)
                    or getattr(getattr(service, "service_type", None), "value", "service"),
                    "service_type": getattr(
                        getattr(service, "service_type", None), "value", None
                    ),
                    "s_vlan": getattr(service, "s_vlan", None),
                    "c_vlan": getattr(service, "c_vlan", None),
                    "gem_port_id": getattr(service, "gem_port_id", None),
                    "connection_type": getattr(
                        getattr(service, "connection_type", None), "value", None
                    ),
                }
                for service in services
            ],
            "missing_vlans": missing_vlans,
            "extra_vlans": extra_vlans,
        }

    def provisioning_form_defaults(
        self,
        db: Session,
        *,
        ont: object,
        profile: object | None,
    ) -> dict[str, object]:
        """Return adapter-derived defaults for ONT provisioning form validation."""
        if profile is None:
            return {}

        from app.services import web_network_onts as web_network_onts_service

        def _enum_value(raw: object) -> str | None:
            value = getattr(raw, "value", raw)
            text = str(value or "").strip()
            return text or None

        vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
        vlan_id_by_tag = {
            int(vlan.tag): str(vlan.id)
            for vlan in vlans
            if getattr(vlan, "tag", None) is not None
        }
        active_services = [
            svc
            for svc in (getattr(profile, "wan_services", None) or [])
            if getattr(svc, "is_active", False)
        ]
        primary_service = active_services[0] if active_services else None
        wan_protocol = _enum_value(getattr(primary_service, "connection_type", None))
        if wan_protocol == "bridge":
            wan_protocol = "bridged"

        mgmt_vlan_tag = getattr(profile, "mgmt_vlan_tag", None)
        wan_vlan_tag = getattr(primary_service, "s_vlan", None) if primary_service else None

        return {
            "onu_mode": _enum_value(getattr(profile, "onu_mode", None)),
            "mgmt_ip_mode": _enum_value(getattr(profile, "mgmt_ip_mode", None)),
            "mgmt_vlan_id": vlan_id_by_tag.get(int(mgmt_vlan_tag))
            if mgmt_vlan_tag is not None
            else None,
            "wan_protocol": wan_protocol,
            "wan_vlan_id": vlan_id_by_tag.get(int(wan_vlan_tag))
            if wan_vlan_tag is not None
            else None,
            "wifi_enabled": getattr(profile, "wifi_enabled", None),
            "wifi_ssid": getattr(profile, "wifi_ssid_template", None),
            "wifi_security_mode": getattr(profile, "wifi_security_mode", None),
            "wifi_channel": getattr(profile, "wifi_channel", None),
        }


service_intent_ui_adapter = ServiceIntentUiAdapter()
adapter_registry.register(service_intent_ui_adapter)
