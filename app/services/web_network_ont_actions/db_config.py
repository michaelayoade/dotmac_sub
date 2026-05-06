"""Database configuration management for ONT web actions."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services import network as network_service
from app.services.credential_crypto import encrypt_credential
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_desired_config import set_desired_config_values
from app.services.network.ont_management_ipam import (
    allocate_ont_management_ip,
    release_ont_management_ip,
)
from app.services.web_network_ont_actions._common import (
    _is_input_error,
    _log_action_audit,
)
from app.services.web_network_ont_actions.config_setters import (
    set_lan_config,
    set_mgmt_remote_access,
    set_wan_config,
    set_wifi_config,
)


def _delivery_pending_result(result: ActionResult) -> ActionResult:
    """Treat saved desired config as pending when only ACS delivery failed."""
    if result.success or _is_input_error(result.message):
        return result

    data = dict(result.data or {})
    if data.get("delivery_pending") is False:
        return result

    text = (result.message or "").lower()
    if "no acs server configured" in text:
        return result

    data["delivery_pending"] = True
    data.setdefault("waiting_reason", "next_inform")
    reason = (result.message or "Device is not reachable through ACS.").strip()
    return ActionResult(
        success=True,
        message=f"saved, waiting for device inform to apply ({reason})",
        data=data,
        waiting=True,
    )


def update_ont_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str | None = None,
    config_method: str | None = None,
    ip_protocol: str | None = None,
    wan_static_ip: str | None = None,
    wan_static_subnet: str | None = None,
    wan_static_gateway: str | None = None,
    wan_static_dns: str | None = None,
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
    wifi_enabled: bool | None = None,
    wifi_ssid: str | None = None,
    wifi_channel: str | None = None,
    wifi_security_mode: str | None = None,
    wifi_password: str | None = None,
    voip_enabled: bool | None = None,
    push_to_device: bool = False,
    push_wan: bool = True,
    push_lan: bool = True,
    push_mgmt: bool = True,
    push_wifi: bool = True,
    request: Request | None = None,
) -> ActionResult:
    """Update ONT configuration fields in the database, optionally push to device."""

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    # Management reachability is controlled by desired_config access intent.
    mgmt_access_enabled: bool | None = None
    if mgmt_ip_mode is not None:
        mgmt_access_enabled = mgmt_ip_mode in {"dhcp", "static_ip"}
    elif mgmt_remote_access is not None:
        mgmt_access_enabled = mgmt_remote_access
    if voip_enabled is not None:
        ont.voip_enabled = voip_enabled

    try:
        desired_updates = {}
        if wan_mode is not None:
            wan_mode_value = wan_mode.strip() or None
            desired_updates["wan.mode"] = wan_mode_value
            if wan_mode_value != "pppoe":
                desired_updates["wan.pppoe_username"] = None
                desired_updates["wan.pppoe_password"] = None
        if ip_protocol is not None:
            desired_updates["wan.ip_protocol"] = ip_protocol.strip() or None
        if wan_static_ip is not None:
            desired_updates["wan.static_ip"] = wan_static_ip.strip() or None
        if wan_static_subnet is not None:
            desired_updates["wan.static_subnet"] = wan_static_subnet.strip() or None
        if wan_static_gateway is not None:
            desired_updates["wan.static_gateway"] = wan_static_gateway.strip() or None
        if wan_static_dns is not None:
            desired_updates["wan.static_dns"] = wan_static_dns.strip() or None
        if pppoe_username is not None:
            desired_updates["wan.pppoe_username"] = pppoe_username.strip() or None
        management_mode = mgmt_ip_mode.strip() if mgmt_ip_mode is not None else None
        management_address = (
            mgmt_ip_address.strip() if mgmt_ip_address is not None else None
        )
        management_allocation = None
        if management_mode == "static_ip":
            try:
                management_allocation = allocate_ont_management_ip(
                    db,
                    ont=ont,
                    requested_ip=management_address,
                )
            except ValueError as exc:
                db.rollback()
                return ActionResult(success=False, message=str(exc))
            management_address = management_allocation.address
        elif management_mode in {"inactive", "dhcp"}:
            release_ont_management_ip(db, ont=ont, mode=management_mode)
        if mgmt_ip_mode is not None:
            desired_updates["management.ip_mode"] = management_mode or None
        if mgmt_ip_address is not None:
            desired_updates["management.ip_address"] = management_address or None
        if management_mode == "static_ip" and management_allocation is not None:
            desired_updates["management.subnet"] = (
                management_allocation.subnet
            )
            desired_updates["management.gateway"] = (
                management_allocation.gateway
            )
        elif management_mode in {"inactive", "dhcp"}:
            desired_updates["management.subnet"] = None
            desired_updates["management.gateway"] = None
            desired_updates["management.vlan"] = None
            desired_updates["management.vlan_id"] = None
        if mgmt_access_enabled is not None:
            desired_updates["access.mgmt_remote"] = mgmt_access_enabled
        if lan_gateway_ip is not None:
            desired_updates["lan.ip"] = lan_gateway_ip.strip() or None
        if lan_subnet_mask is not None:
            desired_updates["lan.subnet"] = lan_subnet_mask.strip() or None
        if lan_dhcp_enabled is not None:
            desired_updates["lan.dhcp_enabled"] = lan_dhcp_enabled
        if lan_dhcp_start is not None:
            desired_updates["lan.dhcp_start"] = lan_dhcp_start.strip() or None
        if lan_dhcp_end is not None:
            desired_updates["lan.dhcp_end"] = lan_dhcp_end.strip() or None
        if wifi_enabled is not None:
            desired_updates["wifi.enabled"] = wifi_enabled
        if wifi_ssid is not None:
            desired_updates["wifi.ssid"] = wifi_ssid.strip() or None
        if wifi_channel is not None:
            desired_updates["wifi.channel"] = wifi_channel.strip() or None
        if wifi_security_mode is not None:
            desired_updates["wifi.security_mode"] = wifi_security_mode.strip() or None
        if pppoe_password:
            desired_updates["wan.pppoe_password"] = encrypt_credential(pppoe_password)
        if wifi_password:
            desired_updates["wifi.password"] = encrypt_credential(wifi_password)
        set_desired_config_values(ont, desired_updates)
    except ValueError as exc:
        db.rollback()
        return ActionResult(success=False, message=str(exc))

    db.add(ont)
    db.flush()

    push_messages: list[str] = []
    push_success = True
    push_waiting = False

    if push_to_device:
        # Remote ACS writes can block while the CPE is slow to consume tasks.
        # Persist the desired intent before those calls so the request does not
        # hold an idle database transaction open for the duration of TR-069
        # polling.
        db.commit()

        wan_push_requested = push_wan and any(
            value is not None
            for value in (
                wan_mode,
                config_method,
                ip_protocol,
                wan_static_ip,
                wan_static_subnet,
                wan_static_gateway,
                wan_static_dns,
                pppoe_username,
                pppoe_password,
            )
        )
        if wan_push_requested:
            action_wan_mode = (
                "bridge" if wan_mode in {"setup_via_onu", "bridged"} else wan_mode
            )
            if action_wan_mode == "static_ip":
                action_wan_mode = "static"
            result = set_wan_config(
                db,
                ont_id,
                wan_mode=action_wan_mode or "dhcp",
                pppoe_username=pppoe_username.strip() if pppoe_username else None,
                pppoe_password=pppoe_password.strip() if pppoe_password else None,
                ip_address=wan_static_ip.strip() if wan_static_ip else None,
                subnet_mask=wan_static_subnet.strip() if wan_static_subnet else None,
                gateway=wan_static_gateway.strip() if wan_static_gateway else None,
                dns_servers=wan_static_dns.strip() if wan_static_dns else None,
                request=request,
            )
            result = _delivery_pending_result(result)
            push_messages.append(f"WAN: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not result.success:
                push_success = False

        if push_lan and any(
            [
                lan_gateway_ip,
                lan_subnet_mask,
                lan_dhcp_enabled is not None,
                lan_dhcp_start,
                lan_dhcp_end,
            ]
        ):
            result = set_lan_config(
                db,
                ont_id,
                lan_ip=lan_gateway_ip.strip() if lan_gateway_ip else None,
                lan_subnet=lan_subnet_mask.strip() if lan_subnet_mask else None,
                dhcp_enabled=lan_dhcp_enabled,
                dhcp_start=lan_dhcp_start.strip() if lan_dhcp_start else None,
                dhcp_end=lan_dhcp_end.strip() if lan_dhcp_end else None,
                request=request,
            )
            result = _delivery_pending_result(result)
            push_messages.append(f"LAN: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not result.success:
                push_success = False

        if push_mgmt and mgmt_ip_mode is not None:
            result = set_mgmt_remote_access(
                db,
                ont_id,
                enabled=bool(mgmt_access_enabled),
                request=request,
            )
            push_messages.append(f"Management: {result.message}")
            push_waiting = push_waiting or result.waiting
            if not result.success:
                push_success = False

        if push_wifi and any(
            [
                wifi_enabled is not None,
                wifi_ssid,
                wifi_password,
                wifi_security_mode,
                wifi_channel,
            ]
        ):
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
            result = _delivery_pending_result(result)
            push_messages.append(f"WiFi: {result.message}")
            push_waiting = push_waiting or result.waiting
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
        if push_waiting:
            set_desired_config_values(ont, {"delivery.pending_apply": True})
            db.add(ont)
            db.flush()
        elif push_success:
            set_desired_config_values(ont, {"delivery.pending_apply": None})
            db.add(ont)
            db.flush()
        if push_messages:
            message = "Configuration saved. " + "; ".join(push_messages)
        else:
            message = "Configuration saved. No device-delivered fields changed."
        return ActionResult(success=push_success, message=message, waiting=push_waiting)

    return ActionResult(success=True, message="Configuration saved.")


def set_voip_enabled(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    request: Request | None = None,
) -> ActionResult:
    """Set VoIP enabled status on ONT."""
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    ont.voip_enabled = enabled
    db.commit()

    status = "enabled" if enabled else "disabled"
    _log_action_audit(
        db,
        request=request,
        action="set_voip_enabled",
        ont_id=ont_id,
        metadata={"voip_enabled": enabled},
    )
    return ActionResult(success=True, message=f"VoIP {status} on {ont.serial_number}")
