"""Service helpers for remote ONT action web routes."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntProvisioningStatus, OntUnit
from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network as network_service
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.ont_actions import ActionResult, OntActions
from app.services.network_operations import run_tracked_action

logger = logging.getLogger(__name__)


def _normalize_fsp(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw.lower().startswith("pon-"):
        raw = raw[4:].strip()
    return raw or None


def _parse_ont_id_on_olt(external_id: str | None) -> int | None:
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    if ":" in ext:
        suffix = ext.rsplit(":", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _resolve_return_olt_context(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, OLTDevice | None, str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)

    olt = db.get(OLTDevice, str(ont.olt_device_id)) if ont.olt_device_id else None
    board = (ont.board or "").strip()
    port = (ont.port or "").strip()
    fsp = _normalize_fsp(f"{board}/{port}") if board and port else None
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
    return ont, olt, fsp, ont_id_on_olt


def execute_reboot(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Execute reboot action with operation tracking."""
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_reboot,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.reboot(db, ont_id),
        correlation_key=f"ont_reboot:{ont_id}",
        initiated_by=initiated_by,
    )

    # Emit audit event for reboot operation
    if result.success:
        try:
            ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
            emit_event(
                db,
                EventType.ont_rebooted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(ont.olt_device_id) if ont and ont.olt_device_id else None,
                    "method": "tr069",
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_rebooted event: %s", e)

    return result


def execute_refresh(db: Session, ont_id: str) -> ActionResult:
    """Execute status refresh and return result."""
    return OntActions.refresh_status(db, ont_id)


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


def execute_factory_reset(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Execute factory reset with operation tracking."""
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_factory_reset,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.factory_reset(db, ont_id),
        correlation_key=f"ont_factory_reset:{ont_id}",
        initiated_by=initiated_by,
    )

    # Emit audit event for factory reset operation
    if result.success:
        try:
            ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
            emit_event(
                db,
                EventType.ont_factory_reset,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(ont.olt_device_id) if ont and ont.olt_device_id else None,
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_factory_reset event: %s", e)

    return result


def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
    """Set WiFi SSID and return result."""
    return OntActions.set_wifi_ssid(db, ont_id, ssid)


def set_wifi_password(db: Session, ont_id: str, password: str) -> ActionResult:
    """Set WiFi password and return result."""
    return OntActions.set_wifi_password(db, ont_id, password)


def toggle_lan_port(db: Session, ont_id: str, port: int, enabled: bool) -> ActionResult:
    """Toggle a LAN port and return result."""
    return OntActions.toggle_lan_port(db, ont_id, port, enabled)


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    username: str,
    password: str,
    *,
    initiated_by: str | None = None,
) -> ActionResult:
    """Push PPPoE credentials to ONT via TR-069 with operation tracking."""
    return run_tracked_action(
        db,
        NetworkOperationType.ont_set_pppoe,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.set_pppoe_credentials(db, ont_id, username, password),
        correlation_key=f"ont_set_pppoe:{ont_id}",
        initiated_by=initiated_by,
    )


def run_ping_diagnostic(
    db: Session, ont_id: str, host: str, count: int = 4
) -> ActionResult:
    """Run ping diagnostic from ONT via TR-069."""
    return OntActions.run_ping_diagnostic(db, ont_id, host, count)


def run_traceroute_diagnostic(db: Session, ont_id: str, host: str) -> ActionResult:
    """Run traceroute diagnostic from ONT via TR-069."""
    return OntActions.run_traceroute_diagnostic(db, ont_id, host)


def execute_enable_ipv6(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Enable IPv6 dual-stack on ONT with operation tracking."""
    from app.services.network.ont_action_network import enable_ipv6_on_wan

    return run_tracked_action(
        db,
        NetworkOperationType.ont_enable_ipv6,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: enable_ipv6_on_wan(db, ont_id),
        correlation_key=f"ont_enable_ipv6:{ont_id}",
        initiated_by=initiated_by,
    )


def execute_omci_reboot(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> tuple[bool, str]:
    """Reboot ONT via OMCI through the OLT."""
    from app.services.network.olt_ssh_ont import reboot_ont_omci
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"

    ok, msg = reboot_ont_omci(olt, fsp, olt_ont_id)

    # Emit audit event for reboot operation
    if ok:
        try:
            emit_event(
                db,
                EventType.ont_rebooted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "fsp": fsp,
                    "ont_id_on_olt": olt_ont_id,
                    "method": "omci",
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_rebooted event: %s", e)

    return ok, msg


def configure_management_ip(
    db: Session,
    ont_id: str,
    vlan_id: int,
    ip_mode: str = "dhcp",
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP via OLT IPHOST command."""
    from app.services.network.olt_ssh_ont import configure_ont_iphost
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return configure_ont_iphost(
        olt,
        fsp,
        olt_ont_id,
        vlan_id=vlan_id,
        ip_mode=ip_mode,
        ip_address=ip_address,
        subnet=subnet,
        gateway=gateway,
    )


def fetch_iphost_config(db: Session, ont_id: str) -> tuple[bool, str, dict[str, str]]:
    """Fetch ONT IPHOST config from OLT."""
    from app.services.network.olt_ssh_ont import get_ont_iphost_config
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT", {}
    return get_ont_iphost_config(olt, fsp, olt_ont_id)


def bind_tr069_profile(db: Session, ont_id: str, profile_id: int) -> tuple[bool, str]:
    """Bind TR-069 server profile to ONT via OLT."""
    from app.services.network.olt_ssh_ont import bind_tr069_server_profile
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return bind_tr069_server_profile(olt, fsp, olt_ont_id, profile_id)


def return_to_inventory(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> ActionResult:
    """Release an ONT from the OLT, close assignment, and clear service state."""
    from app.services.network.olt_ssh_ont import deauthorize_ont
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port,
        get_service_ports_for_ont,
    )

    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")
    if not olt or not fsp or olt_ont_id is None:
        return ActionResult(
            success=False,
            message="Cannot resolve OLT context for this ONT.",
        )

    ok, msg, service_ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok:
        return ActionResult(
            success=False,
            message=f"Cannot read OLT service-ports before release: {msg}",
        )

    deleted_service_ports = 0
    for service_port in service_ports:
        ok, msg = delete_service_port(olt, service_port.index)
        if not ok:
            return ActionResult(
                success=False,
                message=(
                    "Failed to remove OLT service-port "
                    f"{service_port.index}: {msg}"
                ),
            )
        deleted_service_ports += 1

        # Emit audit event for service port deletion
        try:
            emit_event(
                db,
                EventType.ont_service_port_deleted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "service_port_index": service_port.index,
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_service_port_deleted event: %s", e)

    ok, msg = deauthorize_ont(olt, fsp, olt_ont_id)
    if not ok:
        return ActionResult(
            success=False,
            message=f"Failed to delete ONT from OLT: {msg}",
        )

    # Emit audit event for ONT deauthorization
    try:
        emit_event(
            db,
            EventType.ont_deauthorized,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "fsp": fsp,
                "ont_id_on_olt": olt_ont_id,
            },
            actor=initiated_by or "system",
        )
    except Exception as e:
        logger.warning("Failed to emit ont_deauthorized event: %s", e)

    active_assignment = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.created_at.desc())
        .limit(1)
    ).first()

    if active_assignment is not None:
        active_assignment.active = False

    ont.provisioning_profile_id = None
    ont.provisioning_status = OntProvisioningStatus.unprovisioned
    ont.last_provisioned_at = None
    ont.external_id = None
    ont.wan_vlan_id = None
    ont.wan_mode = None
    ont.config_method = None
    ont.ip_protocol = None
    ont.pppoe_username = None
    ont.pppoe_password = None
    ont.wan_remote_access = False
    ont.tr069_acs_server_id = None
    ont.mgmt_ip_mode = None
    ont.mgmt_vlan_id = None
    ont.mgmt_ip_address = None
    ont.mgmt_remote_access = False
    ont.voip_enabled = False

    db.commit()
    db.refresh(ont)

    assignment_msg = "assignment closed and " if active_assignment is not None else ""
    service_port_msg = (
        f"{deleted_service_ports} service-port(s) removed, "
        if deleted_service_ports
        else ""
    )
    return ActionResult(
        success=True,
        message=(
            "ONT returned to inventory: "
            f"{service_port_msg}{assignment_msg}removed from OLT and service state cleared."
        ),
    )


def fetch_olt_side_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch ONT config from OLT side via SSH (works without GenieACS).

    Returns an ActionResult with data dict containing ont_info, ont_wan,
    and service_ports sections.
    """
    from app.services.network.ont_action_device import get_running_config

    result = get_running_config(db, ont_id)
    if not result.success:
        return ActionResult(success=False, message=result.message)

    data = result.data or {}
    return ActionResult(
        success=True,
        message="OLT-side config retrieved",
        data={
            "ont_info": data.get("device_info", ""),
            "ont_wan": data.get("wan", ""),
            "service_ports": data.get("service_ports", ""),
        },
    )


def fetch_olt_status(db: Session, ont_id: str) -> dict[str, Any]:
    """Query the OLT directly for ONT registration state (GPON layer).

    Returns a dict with success, message, and optional entry data.
    """
    from app.models.network import OntUnit

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return {"success": False, "message": "ONT not found"}

    olt = getattr(ont, "olt_device", None)
    if not olt:
        return {"success": False, "message": "ONT has no associated OLT"}

    return {
        "success": True,
        "message": "ONT status retrieved",
        "entry": {
            "online_status": getattr(ont, "online_status", None),
            "onu_rx_signal_dbm": getattr(ont, "onu_rx_signal_dbm", None),
            "olt_rx_signal_dbm": getattr(ont, "olt_rx_signal_dbm", None),
        },
    }


def resolve_stored_pppoe_password(db: Session, ont_id: str) -> str:
    """Decrypt and return the stored PPPoE password for an ONT."""
    from app.models.network import OntUnit
    from app.services.credential_crypto import decrypt_credential

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return ""

    raw = getattr(ont, "pppoe_password", None)
    if not raw:
        return ""

    try:
        return decrypt_credential(raw) or ""
    except Exception:
        logger.warning("Failed to decrypt PPPoE password for ONT %s", ont_id)
        return ""
