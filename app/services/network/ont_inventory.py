"""ONT inventory lifecycle services."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntProvisioningStatus
from app.services import network as network_service
from app.services.network.cpe import ensure_cpe_for_ont
from app.services.network.ont_actions import ActionResult

logger = logging.getLogger(__name__)


def return_ont_to_inventory(db: Session, ont_id: str) -> ActionResult:
    """Return an ONT to reusable inventory, closing assignments and service state."""
    from app.services.web_network_ont_actions import _cleanup_olt_state_for_return
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
    if needs_olt_cleanup:
        ok, completed, errors = _cleanup_olt_state_for_return(db, ont_id)
        if not ok:
            details = ", ".join(completed + errors)
            return ActionResult(
                success=False,
                message=f"Return to inventory stopped before local cleanup: {details}.",
            )

    for assignment in active_assignments:
        assignment.active = False

    ont.is_active = True
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
    ont.lan_gateway_ip = None
    ont.lan_subnet_mask = None
    ont.lan_dhcp_enabled = None
    ont.lan_dhcp_start = None
    ont.lan_dhcp_end = None
    ont.wifi_ssid = None
    ont.wifi_password = None
    if hasattr(ont, "wifi_enabled"):
        ont.wifi_enabled = None
    if hasattr(ont, "wifi_channel"):
        ont.wifi_channel = None
    if hasattr(ont, "wifi_security_mode"):
        ont.wifi_security_mode = None
    ont.provisioning_steps_completed = None

    db.flush()
    cpe = ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)
    if cpe is not None:
        logger.info("Moved CPE %s to inventory for returned ONT %s", cpe.id, ont.id)

    db.commit()
    db.refresh(ont)

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

    autofind_refresh = refresh_returned_ont_autofind(
        db,
        olt_id=previous_olt_id,
        serial_number=getattr(ont, "serial_number", None),
        fsp=previous_fsp,
    )
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
