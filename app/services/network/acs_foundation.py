"""OLT-side ACS foundation setup for ONT authorization."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.olt_batched_mgmt import BatchedMgmtSpec
from app.services.network.olt_protocol_adapters import get_protocol_adapter

logger = logging.getLogger(__name__)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply_acs_foundation(
    db: Session,
    *,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    ont_id_on_olt: int,
    olt_config_already_applied: bool = False,
) -> dict[str, Any]:
    """Apply the minimum OLT-side foundation required for ACS connectivity.

    This does not wait for ACS inform and does not apply subscriber WAN/PPPoE
    configuration. The ONT should inform asynchronously once it boots, gets a
    management IP, and reaches the configured ACS URL.
    """
    steps: list[dict[str, Any]] = []

    ont = db.get(OntUnit, ont_unit_id)
    if not ont:
        logger.warning("ACS foundation: ONT %s not found", ont_unit_id)
        return {"success": False, "message": "ONT not found", "steps": steps}

    olt = db.get(OLTDevice, olt_id)
    if not olt:
        logger.warning(
            "ACS foundation: OLT %s not found for ONT %s",
            olt_id,
            ont_unit_id,
        )
        return {"success": False, "message": "OLT not found", "steps": steps}

    effective_config = resolve_effective_ont_config(db, ont)
    effective_values = effective_config.get("values", {})
    config_pack = effective_config.get("config_pack")

    mgmt_ip = effective_values.get("mgmt_ip_address")
    mgmt_vlan_tag = _int_or_none(effective_values.get("mgmt_vlan"))
    mgmt_gem_index = _int_or_none(getattr(config_pack, "mgmt_gem_index", None)) or 2
    mgmt_subnet = effective_values.get("mgmt_subnet") or "255.255.255.0"
    mgmt_gateway = effective_values.get("mgmt_gateway")
    tr069_profile_id = _int_or_none(effective_values.get("tr069_olt_profile_id"))
    acs_server_id = effective_values.get("tr069_acs_server_id")
    internet_config_ip_index = _int_or_none(
        effective_values.get("internet_config_ip_index")
    )
    if internet_config_ip_index is None and mgmt_vlan_tag is not None:
        internet_config_ip_index = 0

    if olt_config_already_applied:
        steps.append({
            "name": "Run batched OLT management setup",
            "success": True,
            "message": "OLT management/TR-069 config already applied during authorization",
            "skipped": True,
        })
    elif not mgmt_ip:
        steps.append({
            "name": "Configure management IP",
            "success": True,
            "message": "No static management IP allocated; configuring DHCP IPHOST on the management VLAN",
        })
    elif mgmt_vlan_tag is None:
        steps.append({
            "name": "Configure management IP",
            "success": True,
            "message": "No management VLAN configured, skipping IPHOST config",
            "skipped": True,
        })

    if tr069_profile_id and mgmt_vlan_tag is None:
        message = (
            "TR-069 profile is configured, but no management VLAN was resolved. "
            "Refusing to bind ACS because the ONT would not have a reachable "
            "management path for informs or connection requests."
        )
        steps.append({
            "name": "Resolve ACS management path",
            "success": False,
            "message": message,
        })
        raise RuntimeError(message)

    spec = BatchedMgmtSpec(
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        mgmt_vlan_tag=mgmt_vlan_tag,
        mgmt_gem_index=mgmt_gem_index,
        ip_mode="static" if mgmt_ip else "dhcp",
        ip_address=str(mgmt_ip) if mgmt_ip else None,
        subnet_mask=str(mgmt_subnet) if mgmt_ip else None,
        gateway=str(mgmt_gateway) if mgmt_ip and mgmt_gateway else None,
        internet_config_ip_index=internet_config_ip_index
        if mgmt_vlan_tag is not None
        else None,
        tr069_profile_id=tr069_profile_id,
    )

    if acs_server_id and not tr069_profile_id:
        message = (
            "ACS is configured for this ONT, but no OLT TR-069 profile ID "
            "was resolved. The ONT cannot be bound to ACS until the OLT "
            "config pack or desired config provides tr069_olt_profile_id."
        )
        steps.append({
            "name": "Resolve TR-069 OLT profile",
            "success": False,
            "message": message,
        })
        raise RuntimeError(message)

    ont_serial = str(ont.serial_number or ont_unit_id)
    olt_name = str(olt.name or olt_id)

    if not olt_config_already_applied and not any(
        (
            spec.has_service_port,
            spec.has_iphost,
            spec.has_internet_config,
            spec.has_tr069,
        )
    ):
        logger.info(
            "ACS foundation: No OLT-side management config for ONT %s, skipping",
            ont.serial_number,
        )
        steps.append({
            "name": "Run batched OLT management setup",
            "success": True,
            "message": "No management IP or TR-069 profile configured",
            "skipped": True,
        })
        return {
            "success": True,
            "message": "No OLT-side management config configured",
            "steps": steps,
            "skipped": True,
        }

    if not olt_config_already_applied:
        logger.info(
            "ACS foundation: Running batched OLT management setup for ONT %s on OLT %s",
            ont_serial,
            olt_name,
        )
        adapter = get_protocol_adapter(olt)
        # Eagerly load all OLT columns before detaching from session
        for column in OLTDevice.__table__.columns:
            getattr(olt, column.key)
        db.expunge(olt)
        # Execute SSH commands outside the transaction
        batch_result = adapter.configure_management_batch(spec)
        steps.append({
            "name": "Run batched OLT management setup",
            "success": batch_result.success,
            "message": batch_result.message,
            "data": batch_result.data,
            "mgmt_ip": mgmt_ip,
            "mgmt_vlan": mgmt_vlan_tag,
            "tr069_profile_id": tr069_profile_id,
        })
        if not batch_result.success:
            logger.warning(
                "ACS foundation: Batched OLT management setup failed for ONT %s: %s",
                ont_serial,
                batch_result.message,
            )
            raise RuntimeError(
                f"Batched OLT management setup failed: {batch_result.message}"
            )

    return {
        "success": True,
        "message": (
            "OLT management and TR-069 profile setup completed; "
            "ACS inform will be handled asynchronously."
        ),
        "steps": steps,
    }
