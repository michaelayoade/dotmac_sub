"""Database configuration management for ONT web actions."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import MgmtIpMode, OntAssignment, OnuMode, WanMode
from app.services import network as network_service
from app.services.credential_crypto import encrypt_credential
from app.services.network.ont_actions import ActionResult
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
)
from app.services.web_network_ont_actions.config_setters import (
    set_lan_config,
    set_wifi_config,
)


def _active_assignment_for_ont(db: Session, ont) -> OntAssignment:
    for assignment in getattr(ont, "assignments", []) or []:
        if getattr(assignment, "active", False):
            return assignment
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db.add(assignment)
    return assignment


def update_ont_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str | None = None,
    config_method: str | None = None,
    ip_protocol: str | None = None,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    mgmt_ip_mode: str | None = None,
    mgmt_ip_address: str | None = None,
    mgmt_remote_access: bool | None = None,
    lan_gateway_ip: str | None = None,
    lan_subnet_mask: str | None = None,
    lan_dhcp_enabled: bool | None = None,
    lan_dhcp_start: str | None = None,
    lan_dhcp_end: str | None = None,
    wifi_enabled: bool = True,
    wifi_ssid: str | None = None,
    wifi_channel: str | None = None,
    wifi_security_mode: str | None = None,
    wifi_password: str | None = None,
    voip_enabled: bool | None = None,
    push_to_device: bool = False,
    request: Request | None = None,
) -> ActionResult:
    """Update ONT configuration fields in the database, optionally push to device."""

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    # Update direct ONT runtime fields that still live on the ONT model.
    if mgmt_remote_access is not None:
        ont.mgmt_remote_access = mgmt_remote_access
    if lan_gateway_ip is not None:
        ont.lan_gateway_ip = lan_gateway_ip.strip() or None
    if lan_subnet_mask is not None:
        ont.lan_subnet_mask = lan_subnet_mask.strip() or None
    ont.lan_dhcp_enabled = lan_dhcp_enabled
    if lan_dhcp_start is not None:
        ont.lan_dhcp_start = lan_dhcp_start.strip() or None
    if lan_dhcp_end is not None:
        ont.lan_dhcp_end = lan_dhcp_end.strip() or None
    if voip_enabled is not None:
        ont.voip_enabled = voip_enabled

    try:
        assignment = _active_assignment_for_ont(db, ont)
        if wan_mode is not None:
            assignment.wan_mode = (
                OnuMode.bridging
                if wan_mode in {WanMode.setup_via_onu.value, "bridge", "bridged"}
                else OnuMode.routing
            )
            assignment.ip_mode = (
                MgmtIpMode.static_ip
                if wan_mode == WanMode.static_ip.value
                else MgmtIpMode.dhcp
            )
        if pppoe_username is not None:
            assignment.pppoe_username = pppoe_username.strip() or None
        if pppoe_password:
            assignment.pppoe_password = encrypt_credential(pppoe_password)
        if lan_gateway_ip is not None:
            assignment.lan_ip = lan_gateway_ip.strip() or None
        if lan_subnet_mask is not None:
            assignment.lan_subnet = lan_subnet_mask.strip() or None
        assignment.lan_dhcp_enabled = lan_dhcp_enabled
        if lan_dhcp_start is not None:
            assignment.lan_dhcp_start = lan_dhcp_start.strip() or None
        if lan_dhcp_end is not None:
            assignment.lan_dhcp_end = lan_dhcp_end.strip() or None
        if mgmt_ip_mode is not None:
            assignment.mgmt_ip_mode = (
                MgmtIpMode.static_ip
                if mgmt_ip_mode == "static_ip"
                else MgmtIpMode.dhcp
                if mgmt_ip_mode == "dhcp"
                else MgmtIpMode.inactive
            )
        if mgmt_ip_address is not None:
            assignment.mgmt_ip_address = mgmt_ip_address.strip() or None
        assignment.wifi_enabled = wifi_enabled
        if wifi_ssid is not None:
            assignment.wifi_ssid = wifi_ssid.strip() or None
        assignment.wifi_channel = wifi_channel
        assignment.wifi_security_mode = wifi_security_mode
        if wifi_password:
            assignment.wifi_password = encrypt_credential(wifi_password)
    except ValueError as exc:
        db.rollback()
        return ActionResult(success=False, message=str(exc))

    db.add(ont)
    db.flush()

    push_messages: list[str] = []
    push_success = True

    if push_to_device:
        wan_push_requested = any(
            value is not None
            for value in (
                wan_mode,
                config_method,
                ip_protocol,
                pppoe_username,
                pppoe_password,
            )
        )
        if wan_push_requested:
            push_messages.append(
                "WAN: direct WAN/PPPoE pushes are disabled; provision the active "
                "WAN service instance instead."
            )
            push_success = False

        if any([lan_gateway_ip, lan_subnet_mask, lan_dhcp_enabled is not None]):
            result = set_lan_config(
                db,
                ont_id,
                lan_ip=ont.lan_gateway_ip,
                lan_subnet=ont.lan_subnet_mask,
                dhcp_enabled=ont.lan_dhcp_enabled,
                dhcp_start=ont.lan_dhcp_start,
                dhcp_end=ont.lan_dhcp_end,
                request=request,
            )
            push_messages.append(f"LAN: {result.message}")
            if not result.success:
                push_success = False

        if any([wifi_ssid, wifi_password, wifi_security_mode, wifi_channel]):
            channel_int: int | None = None
            if wifi_channel:
                try:
                    channel_int = int(wifi_channel)
                except ValueError:
                    pass
            result = set_wifi_config(
                db,
                ont_id,
                enabled=wifi_enabled,
                ssid=wifi_ssid.strip() if wifi_ssid else None,
                password=wifi_password.strip() if wifi_password else None,
                channel=channel_int,
                security_mode=wifi_security_mode.strip()
                if wifi_security_mode
                else None,
                request=request,
            )
            push_messages.append(f"WiFi: {result.message}")
            if not result.success:
                push_success = False

    _log_action_audit(
        db,
        request=request,
        action="update_ont_config",
        ont_id=ont_id,
        metadata={
            "wan_mode": wan_mode,
            "pppoe_username": pppoe_username,
            "wifi_ssid": wifi_ssid,
            "push_to_device": push_to_device,
            "push_success": push_success if push_to_device else None,
        },
    )

    if push_to_device:
        message = "Configuration saved. " + "; ".join(push_messages)
        return ActionResult(success=push_success, message=message)

    return ActionResult(success=True, message="Configuration saved to database.")
