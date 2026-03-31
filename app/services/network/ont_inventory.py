"""ONT inventory lifecycle services."""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    OntAssignment,
    OntProvisioningStatus,
)
from app.services import network as network_service
from app.services.network.ont_actions import ActionResult

logger = logging.getLogger(__name__)


def return_ont_to_inventory(db: Session, ont_id: str) -> ActionResult:
    """Deactivate an ONT, close active assignments, and clear service state."""
    from app.models.ont_autofind import OltAutofindCandidate
    from app.services.web_network_ont_actions import _cleanup_olt_state_for_return
    from app.services.web_network_ont_autofind import sync_olt_autofind_candidates

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    previous_olt_db_id = getattr(ont, "olt_device_id", None)
    previous_olt_id = str(previous_olt_db_id) if previous_olt_db_id else None
    previous_fsp = None
    if getattr(ont, "board", None) and getattr(ont, "port", None):
        previous_fsp = f"{ont.board}/{ont.port}"
    normalized_serial = re.sub(
        r"[^A-Za-z0-9]", "", str(getattr(ont, "serial_number", "") or "")
    ).upper()

    active_assignments = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.is_active.is_(True),
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
    if needs_olt_cleanup:
        ok, completed, errors = _cleanup_olt_state_for_return(db, ont_id)
        if not ok:
            details = ", ".join(completed + errors)
            return ActionResult(
                success=False,
                message=f"Return to inventory stopped before local cleanup: {details}.",
            )

    for assignment in active_assignments:
        assignment.is_active = False

    ont.is_active = False
    ont.olt_device_id = None
    ont.board = None
    ont.port = None
    ont.external_id = None
    ont.provisioning_profile_id = None
    ont.provisioning_status = OntProvisioningStatus.unprovisioned
    ont.last_provisioned_at = None
    ont.authorization_status = None
    ont.wan_vlan_id = None
    ont.wan_mode = None
    ont.config_method = None
    ont.ip_protocol = None
    ont.pppoe_username = None
    ont.pppoe_password = None
    ont.mac_address = None
    ont.observed_wan_ip = None
    ont.observed_pppoe_status = None
    ont.observed_lan_mode = None
    ont.observed_wifi_clients = None
    ont.observed_lan_hosts = None
    ont.observed_runtime_updated_at = None
    ont.wan_remote_access = False
    ont.tr069_acs_server_id = None
    ont.mgmt_ip_mode = None
    ont.mgmt_vlan_id = None
    ont.mgmt_ip_address = None
    ont.mgmt_remote_access = False
    ont.voip_enabled = False
    ont.provisioning_steps_completed = None

    cpe_deactivated = False
    serial_number = str(getattr(ont, "serial_number", "") or "").strip()[:120]
    if serial_number:
        cpe = db.scalars(
            select(CPEDevice)
            .where(CPEDevice.serial_number == serial_number)
            .order_by(CPEDevice.updated_at.desc())
            .limit(1)
        ).first()
        if cpe and cpe.status != DeviceStatus.inactive:
            cpe.status = DeviceStatus.inactive
            cpe_deactivated = True
            logger.info("Deactivated CPE %s for returned ONT %s", cpe.id, ont.id)

    db.commit()
    db.refresh(ont)

    parts = []
    if active_assignment is not None and getattr(active_assignment, "pon_port_id", None):
        parts.append("OLT service state removed")
    if active_assignments:
        assignment_count = len(active_assignments)
        parts.append(
            "assignment closed"
            if assignment_count == 1
            else f"{assignment_count} assignments closed"
        )
    if cpe_deactivated:
        parts.append("CPE deactivated")
    parts.append("identity cleared for rediscovery")
    parts.append("service state cleared")

    if previous_olt_id and previous_olt_db_id is not None:
        sync_ok, sync_message, _sync_stats = sync_olt_autofind_candidates(
            db, previous_olt_id
        )
        if sync_ok:
            rediscovered = next(
                (
                    candidate
                    for candidate in db.scalars(
                        select(OltAutofindCandidate).where(
                            OltAutofindCandidate.olt_id == previous_olt_db_id,
                            OltAutofindCandidate.is_active.is_(True),
                        )
                    ).all()
                    if re.sub(
                        r"[^A-Za-z0-9]",
                        "",
                        str(candidate.serial_number or ""),
                    ).upper()
                    == normalized_serial
                    and (not previous_fsp or str(candidate.fsp or "").strip() == previous_fsp)
                ),
                None,
            )
            if rediscovered is not None:
                parts.append("autofind refreshed and device rediscovered")
            else:
                parts.append("autofind refreshed; device not yet rediscovered")
        else:
            parts.append(f"autofind refresh failed: {sync_message}")

    return ActionResult(
        success=True,
        message=(
            f"ONT returned to inventory: {', '.join(parts)}. "
            "Restart or power-cycle the device for changes to take effect; "
            "after it comes back up, autofind can discover it again."
        ),
    )
