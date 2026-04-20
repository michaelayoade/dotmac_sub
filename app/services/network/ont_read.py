"""Unified ONT read facade composing DB + polling + TR-069 into enriched responses."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit
from app.services.network.ont_action_common import get_ont_strict_or_error
from app.services.network.ont_status import resolve_ont_status_for_model

logger = logging.getLogger(__name__)

# Signal quality thresholds (dBm) — matches olt_polling.py conventions
_SIGNAL_GOOD = -25.0
_SIGNAL_WARNING = -28.0


def _classify_signal(dbm: float | None) -> str | None:
    """Classify OLT RX signal into good/warning/critical."""
    if dbm is None:
        return None
    if dbm >= _SIGNAL_GOOD:
        return "good"
    if dbm >= _SIGNAL_WARNING:
        return "warning"
    return "critical"


def _blank_unknown_status(value: str | None) -> str | None:
    return None if value == "unknown" else value


class OntReadFacade:
    """Unified ONT reader composing multiple data sources."""

    @staticmethod
    def get_enriched(
        db: Session, ont_id: str, *, live_query: bool = False
    ) -> dict[str, Any]:
        """Compose enriched ONT detail from DB + signal + subscriber + capabilities.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            live_query: If True, also query TR-069 live data and persist observed runtime.
        """
        ont, err = get_ont_strict_or_error(db, ont_id)
        if err:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=err.message)
        if ont is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="ONT not found.")

        # Base fields from DB
        status_snapshot = resolve_ont_status_for_model(ont)
        olt_status = _blank_unknown_status(status_snapshot.olt_status.value)
        effective_status = _blank_unknown_status(status_snapshot.effective_status.value)
        result: dict[str, Any] = {
            "id": ont.id,
            "serial_number": ont.serial_number,
            "vendor": ont.vendor,
            "model": ont.model,
            "firmware_version": ont.firmware_version,
            "online_status": olt_status,
            "acs_status": status_snapshot.acs_status.value,
            "effective_status": effective_status,
            "effective_status_source": status_snapshot.effective_status_source.value,
            "acs_last_inform_at": status_snapshot.acs_last_inform_at,
            "name": ont.name,
            # Signal
            "olt_rx_signal_dbm": ont.olt_rx_signal_dbm,
            "onu_rx_signal_dbm": ont.onu_rx_signal_dbm,
            "signal_quality": _classify_signal(ont.olt_rx_signal_dbm),
            "distance_meters": ont.distance_meters,
            "signal_updated_at": ont.signal_updated_at,
            # Observed runtime
            "observed_wan_ip": ont.observed_wan_ip,
            "observed_pppoe_status": ont.observed_pppoe_status,
            "observed_runtime_updated_at": ont.observed_runtime_updated_at,
            # Provisioning
            "provisioning_status": (
                ont.provisioning_status.value if ont.provisioning_status else None
            ),
            "provisioning_profile_name": (
                ont.provisioning_profile.name if ont.provisioning_profile else None
            ),
            # Sync metadata
            "last_sync_source": getattr(ont, "last_sync_source", None),
            "last_sync_at": getattr(ont, "last_sync_at", None),
        }

        # Active assignment → subscriber + PON port context
        assignment = db.scalars(
            select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
        ).first()
        if assignment:
            result["subscriber_id"] = assignment.subscriber_id
            result["subscriber_name"] = (
                assignment.subscriber.display_name
                if assignment.subscriber
                and hasattr(assignment.subscriber, "display_name")
                else (
                    assignment.subscriber.name
                    if assignment.subscriber and hasattr(assignment.subscriber, "name")
                    else None
                )
            )
            result["pon_port_name"] = (
                assignment.pon_port.name if assignment.pon_port else None
            )
            result["olt_name"] = ont.olt_device.name if ont.olt_device else None
        else:
            result.update(
                subscriber_id=None,
                subscriber_name=None,
                pon_port_name=None,
                olt_name=ont.olt_device.name if ont.olt_device else None,
            )

        # Live TR-069 query (optional, slower)
        if live_query:
            try:
                from app.services.acs_client import create_acs_state_reader

                reader = create_acs_state_reader()
                summary = reader.get_device_summary(
                    db, ont_id, persist_observed_runtime=True
                )
                if summary and summary.available:
                    wan_ip = (
                        str(summary.wan.get("WAN IP") or "").strip()
                        if summary.wan
                        else ""
                    )
                    pppoe_status = (
                        str(summary.wan.get("Status") or "").strip()
                        if summary.wan
                        else ""
                    )
                    if wan_ip:
                        result["observed_wan_ip"] = wan_ip
                    if pppoe_status:
                        result["observed_pppoe_status"] = pppoe_status
            except Exception as exc:
                logger.warning("Live TR-069 query failed for ONT %s: %s", ont_id, exc)

        # Vendor capabilities
        result["capabilities"] = OntReadFacade.get_capabilities(db, ont_id)

        return result

    @staticmethod
    def get_capabilities(db: Session, ont_id: str) -> dict[str, bool]:
        """Resolve vendor+model → flat capability dict."""
        ont = db.get(OntUnit, ont_id)
        if not ont or not ont.vendor or not ont.model:
            return {}

        from app.services.network.vendor_capabilities import VendorCapabilities

        cap = VendorCapabilities.resolve_capability(
            db, vendor=ont.vendor, model=ont.model, firmware=ont.firmware_version
        )
        if not cap:
            return {}

        features = cap.supported_features or {}
        return {
            "wifi": features.get("wifi", False),
            "voip": features.get("voip", False),
            "catv": features.get("catv", False),
            "iptv": features.get("iptv", False),
            "tr069": features.get("tr069", True),
            "vlan_tagging": cap.supports_vlan_tagging,
            "qinq": cap.supports_qinq,
            "ipv6": cap.supports_ipv6,
        }

    @staticmethod
    def get_tr069_summary(db: Session, ont_id: str) -> dict[str, Any]:
        """Read the ACS/TR-069 summary through the ACS state adapter."""
        from app.services.acs_client import create_acs_state_reader

        summary = create_acs_state_reader().get_device_summary(db, ont_id)
        if not summary or not summary.available:
            return {"available": False, "error": summary.error if summary else None}
        return asdict(summary)

    @staticmethod
    def get_lan_hosts(db: Session, ont_id: str) -> list[dict[str, Any]]:
        """Read LAN hosts through the ACS state adapter."""
        from app.services.acs_client import create_acs_state_reader

        return create_acs_state_reader().get_lan_hosts(db, ont_id)

    @staticmethod
    def get_ethernet_ports(db: Session, ont_id: str) -> list[dict[str, Any]]:
        """Read Ethernet ports through the ACS state adapter."""
        from app.services.acs_client import create_acs_state_reader

        return create_acs_state_reader().get_ethernet_ports(db, ont_id)

    @staticmethod
    def get_vlan_chain_status(db: Session, ont_id: str) -> dict[str, Any]:
        """Delegate to vlan_chain.validate_chain()."""
        from app.services.network.vlan_chain import validate_chain

        result = validate_chain(db, ont_id)
        if hasattr(result, "__dataclass_fields__"):
            return asdict(result)
        return {"valid": False, "errors": ["Unable to validate VLAN chain"]}


ont_read = OntReadFacade()
