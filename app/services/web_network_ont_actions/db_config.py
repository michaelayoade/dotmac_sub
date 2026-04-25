"""Database configuration management for ONT web actions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import Vlan
from app.services import network as network_service
from app.services.network.ont_actions import ActionResult
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
)
from app.services.web_network_ont_actions.config_setters import (
    set_lan_config,
    set_wifi_config,
)
from app.services.network.ont_desired_config import set_desired_config_value


def _resolve_ont_scoped_vlan(
    db: Session,
    *,
    ont_olt_id,
    vlan_id: str,
    field_label: str,
):
    vlan = db.scalars(select(Vlan).where(Vlan.id == vlan_id).limit(1)).first()
    if vlan is None:
        return None, ActionResult(success=False, message=f"{field_label} VLAN not found")
    if ont_olt_id is None:
        return None, ActionResult(
            success=False,
            message=f"{field_label} VLAN requires the ONT to be assigned to an OLT",
        )
    if vlan.olt_device_id != ont_olt_id:
        return None, ActionResult(
            success=False,
            message=f"{field_label} VLAN {vlan.tag} is not configured on this ONT's OLT",
        )
    return vlan, None


def _normalize_override_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value)


def _upsert_override(
    db: Session,
    *,
    ont,
    field_name: str,
    value,
) -> None:
    set_desired_config_value(ont, field_name, _normalize_override_value(value))


def _persist_desired_config_state(
    db: Session,
    *,
    ont,
    wan_mode: str | None,
    wan_vlan_tag: int | None,
    config_method: str | None,
    ip_protocol: str | None,
    pppoe_username: str | None,
    lan_gateway_ip: str | None,
    lan_subnet_mask: str | None,
    lan_dhcp_enabled: bool | None,
    lan_dhcp_start: str | None,
    lan_dhcp_end: str | None,
    mgmt_ip_mode: str | None,
    mgmt_vlan_tag: int | None,
    mgmt_ip_address: str | None,
    wifi_enabled: bool,
    wifi_ssid: str | None,
    wifi_channel: str | None,
    wifi_security_mode: str | None,
) -> None:
    for field_name, value in {
        "device.config_method": config_method,
        "wan.ip_protocol": ip_protocol,
        "wan.mode": wan_mode,
        "wan.vlan": wan_vlan_tag,
        "wan.pppoe_username": pppoe_username,
        "lan.ip": lan_gateway_ip,
        "lan.subnet": lan_subnet_mask,
        "lan.dhcp_enabled": lan_dhcp_enabled,
        "lan.dhcp_start": lan_dhcp_start,
        "lan.dhcp_end": lan_dhcp_end,
        "management.ip_mode": mgmt_ip_mode,
        "management.vlan": mgmt_vlan_tag,
        "management.ip_address": mgmt_ip_address,
        "wifi.enabled": wifi_enabled,
        "wifi.ssid": wifi_ssid,
        "wifi.channel": wifi_channel,
        "wifi.security_mode": wifi_security_mode,
    }.items():
        _upsert_override(db, ont=ont, field_name=field_name, value=value)


def update_ont_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str | None = None,
    wan_vlan_id: str | None = None,
    config_method: str | None = None,
    ip_protocol: str | None = None,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    mgmt_ip_mode: str | None = None,
    mgmt_vlan_id: str | None = None,
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
    resolved_wan_vlan = None
    resolved_mgmt_vlan = None

    # Resolve VLANs
    if wan_vlan_id:
        vlan, vlan_err = _resolve_ont_scoped_vlan(
            db,
            ont_olt_id=ont.olt_device_id,
            vlan_id=wan_vlan_id,
            field_label="WAN",
        )
        if vlan_err:
            return vlan_err
        resolved_wan_vlan = vlan

    if mgmt_vlan_id:
        vlan, vlan_err = _resolve_ont_scoped_vlan(
            db,
            ont_olt_id=ont.olt_device_id,
            vlan_id=mgmt_vlan_id,
            field_label="Management",
        )
        if vlan_err:
            return vlan_err
        resolved_mgmt_vlan = vlan

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

    wan_vlan_tag = None
    if resolved_wan_vlan is not None:
        wan_vlan_tag = (
            int(resolved_wan_vlan.tag) if resolved_wan_vlan.tag is not None else None
        )

    mgmt_vlan_tag = None
    if resolved_mgmt_vlan is not None:
        mgmt_vlan_tag = (
            int(resolved_mgmt_vlan.tag) if resolved_mgmt_vlan.tag is not None else None
        )

    try:
        _persist_desired_config_state(
            db,
            ont=ont,
            wan_mode=wan_mode,
            wan_vlan_tag=wan_vlan_tag,
            config_method=config_method,
            ip_protocol=ip_protocol,
            pppoe_username=pppoe_username.strip() if pppoe_username is not None else None,
            lan_gateway_ip=lan_gateway_ip.strip() if lan_gateway_ip is not None else None,
            lan_subnet_mask=lan_subnet_mask.strip() if lan_subnet_mask is not None else None,
            lan_dhcp_enabled=lan_dhcp_enabled,
            lan_dhcp_start=lan_dhcp_start.strip() if lan_dhcp_start is not None else None,
            lan_dhcp_end=lan_dhcp_end.strip() if lan_dhcp_end is not None else None,
            mgmt_ip_mode=mgmt_ip_mode,
            mgmt_vlan_tag=mgmt_vlan_tag,
            mgmt_ip_address=mgmt_ip_address.strip()
            if mgmt_ip_address is not None
            else None,
            wifi_enabled=wifi_enabled,
            wifi_ssid=wifi_ssid.strip() if wifi_ssid is not None else None,
            wifi_channel=wifi_channel,
            wifi_security_mode=wifi_security_mode,
        )
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
                wan_vlan_id,
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
