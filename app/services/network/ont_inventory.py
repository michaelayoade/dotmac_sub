"""ONT inventory lifecycle services."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntWanServiceInstance,
)
from app.services import network as network_service
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.cpe import ensure_cpe_for_ont
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_desired_config import clear_desired_config
from app.services.network.ont_status import reset_status_for_inventory

logger = logging.getLogger(__name__)


class _ReturnToInventoryStopped(Exception):
    """Internal control-flow exception used to rollback the return savepoint."""


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


def _is_ont_already_absent(message: str | None) -> bool:
    normalized = (message or "").casefold()
    return (
        "ont does not exist" in normalized
        or "the ont does not exist" in normalized
        or "ont not found" in normalized
    )


def _resolve_return_olt_context(
    db: Session, ont_id: str
) -> tuple[object | None, OLTDevice | None, str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    olt = db.get(OLTDevice, str(ont.olt_device_id)) if ont.olt_device_id else None
    board = (ont.board or "").strip()
    port = (ont.port or "").strip()
    fsp = _normalize_fsp(f"{board}/{port}") if board and port else None
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
    return ont, olt, fsp, ont_id_on_olt


def cleanup_olt_state_for_return(
    db: Session, ont_id: str
) -> tuple[bool, list[str], list[str]]:
    """Remove OLT-side service ports and deauthorize an ONT before inventory return."""
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.service_port_allocator import release_all_for_ont

    completed: list[str] = []
    errors: list[str] = []

    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return False, completed, ["ONT not found"]
    if not olt or not fsp or olt_ont_id is None:
        return True, completed, errors

    adapter = get_protocol_adapter(olt)
    ports_result = adapter.get_service_ports_for_ont(fsp, olt_ont_id)
    if not ports_result.success:
        errors.append(f"Cannot read OLT service-ports: {ports_result.message}")
        return False, completed, errors

    service_ports_data = ports_result.data.get("service_ports", [])
    service_ports = service_ports_data if isinstance(service_ports_data, list) else []
    for service_port in service_ports:
        delete_result = adapter.delete_service_port(service_port.index)
        if not delete_result.success:
            errors.append(
                f"Failed to remove service-port {service_port.index}: {delete_result.message}"
            )
            return False, completed, errors
        completed.append(f"Removed service-port {service_port.index}")
        try:
            emit_event(
                db,
                EventType.ont_service_port_deleted,
                {
                    "ont_id": ont_id,
                    "ont_serial": getattr(ont, "serial_number", None),
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "service_port_index": service_port.index,
                },
                actor="system",
            )
        except Exception as exc:
            logger.warning("Failed to emit ont_service_port_deleted event: %s", exc)

    deauth_result = adapter.deauthorize_ont(fsp, olt_ont_id)
    if not deauth_result.success:
        if _is_ont_already_absent(deauth_result.message):
            completed.append("ONT already absent from OLT")
        else:
            errors.append(f"Failed to deauthorize ONT: {deauth_result.message}")
            return False, completed, errors
    else:
        completed.append("Deauthorized ONT from OLT")

    released_allocations = release_all_for_ont(db, ont_id)
    if released_allocations:
        completed.append(f"Released {released_allocations} service-port allocation(s)")

    if not deauth_result.success:
        return True, completed, errors
    try:
        emit_event(
            db,
            EventType.ont_deauthorized,
            {
                "ont_id": ont_id,
                "ont_serial": getattr(ont, "serial_number", None),
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "fsp": fsp,
                "ont_id_on_olt": olt_ont_id,
            },
            actor="system",
        )
    except Exception as exc:
        logger.warning("Failed to emit ont_deauthorized event: %s", exc)

    return True, completed, errors


def reset_ont_service_state(db: Session, ont, *, reason: str = "service_reset") -> None:
    """Clear desired-state and runtime cache for a reusable ONT.

    Args:
        db: Database session
        ont: ONT unit to reset
        reason: Reason for the reset, retained for caller clarity.
    """
    del reason

    clear_desired_config(ont)

    wan_service_instances = db.scalars(
        select(OntWanServiceInstance).where(OntWanServiceInstance.ont_id == ont.id)
    ).all()
    for instance in wan_service_instances:
        db.delete(instance)

    ont.provisioning_status = OntProvisioningStatus.unprovisioned
    ont.last_provisioned_at = None
    ont.authorization_status = None
    ont.mac_address = None
    ont.observed_wan_ip = None
    ont.observed_pppoe_status = None
    ont.observed_lan_mode = None
    ont.observed_wifi_clients = None
    ont.observed_lan_hosts = None
    ont.observed_runtime_updated_at = None
    ont.tr069_last_snapshot = {}
    ont.tr069_last_snapshot_at = None
    ont.olt_observed_snapshot = {}
    ont.olt_observed_snapshot_at = None
    ont.wan_remote_access = False
    ont.tr069_acs_server_id = None
    ont.mgmt_remote_access = False
    ont.voip_enabled = False
    ont.lan_gateway_ip = None
    ont.lan_subnet_mask = None
    ont.lan_dhcp_enabled = None
    ont.lan_dhcp_start = None
    ont.lan_dhcp_end = None
    ont.provisioning_steps_completed = None
    reset_status_for_inventory(ont)
    ont.onu_rx_signal_dbm = None
    ont.olt_rx_signal_dbm = None
    ont.onu_tx_signal_dbm = None
    ont.ont_temperature_c = None
    ont.ont_voltage_v = None
    ont.ont_bias_current_ma = None
    ont.distance_meters = None
    ont.signal_updated_at = None

    return None


def return_ont_to_inventory(db: Session, ont_id: str) -> ActionResult:
    """Return an ONT to reusable inventory, closing assignments and service state."""
    from app.services.web_network_ont_autofind import refresh_returned_ont_autofind

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    previous_olt_db_id = getattr(ont, "olt_device_id", None)
    previous_olt_id = str(previous_olt_db_id) if previous_olt_db_id else None
    previous_fsp = None
    if getattr(ont, "board", None) and getattr(ont, "port", None):
        previous_fsp = f"{ont.board}/{ont.port}"

    active_assignments = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.created_at.desc())
    ).all()
    active_assignment = active_assignments[0] if active_assignments else None

    needs_olt_cleanup = bool(
        (active_assignment is not None and active_assignment.pon_port_id)
        or ont.olt_device_id
        or ont.board
        or ont.port
        or ont.external_id
    )
    cpe = None
    try:
        with db.begin_nested():
            if needs_olt_cleanup:
                ok, completed, errors = cleanup_olt_state_for_return(db, ont_id)
                if not ok:
                    details = ", ".join(completed + errors)
                    raise _ReturnToInventoryStopped(
                        f"Return to inventory stopped before local cleanup: {details}."
                    )

            for assignment in active_assignments:
                assignment.active = False
                assignment.released_at = datetime.now(UTC)
                assignment.release_reason = "returned_to_inventory"

            ont.is_active = True
            ont.olt_device_id = None
            ont.board = None
            ont.port = None
            ont.external_id = None
            reset_ont_service_state(db, ont)

            try:
                from app.models.tr069 import Tr069CpeDevice

                linked_devices = db.scalars(
                    select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont.id)
                ).all()
                for device in linked_devices:
                    device.ont_unit_id = None
            except Exception as exc:
                logger.warning("Failed to clear TR-069 device association: %s", exc)

            db.flush()
            cpe = ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)
            if cpe is not None:
                logger.info(
                    "Moved CPE %s to inventory for returned ONT %s", cpe.id, ont.id
                )
    except _ReturnToInventoryStopped as exc:
        return ActionResult(success=False, message=str(exc))
    except Exception as exc:
        logger.exception(
            "Failed to update DB state during return-to-inventory for ONT %s", ont_id
        )
        return ActionResult(
            success=False,
            message=(
                "OLT cleanup completed but DB inventory update failed: "
                f"{exc}. Retry return-to-inventory to finish local cleanup."
            ),
        )

    db.commit()

    parts = []
    if active_assignment is not None and getattr(
        active_assignment, "pon_port_id", None
    ):
        parts.append("OLT service state removed")
    if active_assignments:
        assignment_count = len(active_assignments)
        parts.append(
            "assignment closed"
            if assignment_count == 1
            else f"{assignment_count} assignments closed"
        )
    if cpe is not None:
        parts.append("CPE moved to inventory")
    parts.append("identity cleared for rediscovery")
    parts.append("service state cleared")

    db.refresh(ont)

    try:
        autofind_refresh = refresh_returned_ont_autofind(
            db,
            olt_id=previous_olt_id,
            serial_number=getattr(ont, "serial_number", None),
            fsp=previous_fsp,
        )
    except Exception as exc:
        logger.warning("Failed to refresh returned ONT autofind: %s", exc)
        autofind_refresh = {"ok": False, "message": str(exc)}

    if autofind_refresh.get("ok"):
        if autofind_refresh.get("rediscovered"):
            parts.append("autofind refreshed and device rediscovered")
        else:
            parts.append("autofind refreshed; device not yet rediscovered")
    else:
        parts.append(f"autofind refresh failed: {autofind_refresh.get('message')}")

    return ActionResult(
        success=True,
        message=(
            f"ONT returned to inventory: {', '.join(parts)}. "
            "Restart or power-cycle the device for changes to take effect; "
            "after it comes back up, autofind can discover it again."
        ),
        data={
            "olt_id": previous_olt_id,
            "fsp": previous_fsp,
            "serial_number": ont.serial_number,
            "autofind_refreshed": autofind_refresh.get("ok"),
            "autofind_rediscovered": autofind_refresh.get("rediscovered"),
            "unconfigured_url": autofind_refresh.get("url"),
        },
    )
