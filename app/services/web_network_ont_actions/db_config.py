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
    _persist_ont_plan_step,
)
from app.services.web_network_ont_actions.config_setters import (
    configure_wan_config,
    set_lan_config,
    set_pppoe_credentials,
    set_wifi_config,
)


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
    mgmt_remote_access: bool = False,
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
    voip_enabled: bool = False,
    push_to_device: bool = False,
    request: Request | None = None,
) -> ActionResult:
    """Update ONT configuration fields in the database, optionally push to device."""
    from app.models.network import ConfigMethod, IpProtocol, MgmtIpMode, WanMode
    from app.services.credential_crypto import decrypt_credential, encrypt_credential
    from app.services.network import ont_provision_steps

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")

    if wan_mode:
        try:
            ont.wan_mode = WanMode(wan_mode)
        except ValueError:
            pass
    elif wan_mode == "":
        ont.wan_mode = None

    if config_method:
        try:
            ont.config_method = ConfigMethod(config_method)
        except ValueError:
            pass
    elif config_method == "":
        ont.config_method = None

    if ip_protocol:
        try:
            ont.ip_protocol = IpProtocol(ip_protocol)
        except ValueError:
            pass
    elif ip_protocol == "":
        ont.ip_protocol = None

    if mgmt_ip_mode:
        try:
            ont.mgmt_ip_mode = MgmtIpMode(mgmt_ip_mode)
        except ValueError:
            pass
    elif mgmt_ip_mode == "":
        ont.mgmt_ip_mode = None

    if wan_vlan_id:
        vlan = db.scalars(select(Vlan).where(Vlan.id == wan_vlan_id).limit(1)).first()
        if vlan and vlan.olt_device_id and ont.olt_device_id:
            if vlan.olt_device_id != ont.olt_device_id:
                return ActionResult(
                    success=False,
                    message=f"WAN VLAN {vlan.tag} is not configured on this ONT's OLT",
                )
        ont.wan_vlan_id = vlan.id if vlan else None
    elif wan_vlan_id == "":
        ont.wan_vlan_id = None

    if mgmt_vlan_id:
        vlan = db.scalars(select(Vlan).where(Vlan.id == mgmt_vlan_id).limit(1)).first()
        if vlan and vlan.olt_device_id and ont.olt_device_id:
            if vlan.olt_device_id != ont.olt_device_id:
                return ActionResult(
                    success=False,
                    message=f"Management VLAN {vlan.tag} is not configured on this ONT's OLT",
                )
        ont.mgmt_vlan_id = vlan.id if vlan else None
    elif mgmt_vlan_id == "":
        ont.mgmt_vlan_id = None

    if pppoe_username is not None:
        ont.pppoe_username = pppoe_username.strip() or None
    if pppoe_password is not None and pppoe_password.strip():
        ont.pppoe_password = encrypt_credential(pppoe_password.strip())
    if mgmt_ip_address is not None:
        ont.mgmt_ip_address = mgmt_ip_address.strip() or None
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
    ont.voip_enabled = voip_enabled

    if wifi_ssid is not None:
        ont.wifi_ssid = wifi_ssid.strip() or None
    if hasattr(ont, "wifi_enabled"):
        ont.wifi_enabled = wifi_enabled
    if wifi_channel is not None and hasattr(ont, "wifi_channel"):
        ont.wifi_channel = wifi_channel.strip() or None
    if wifi_security_mode is not None and hasattr(ont, "wifi_security_mode"):
        ont.wifi_security_mode = wifi_security_mode.strip() or None

    db.add(ont)
    db.flush()

    push_messages: list[str] = []
    push_success = True

    if push_to_device:
        config_method_value = getattr(getattr(ont, "config_method", None), "value", None)
        wan_mode_value = getattr(getattr(ont, "wan_mode", None), "value", None)

        if config_method_value == "omci" and wan_mode_value == "pppoe":
            wan_vlan_tag = None
            if ont.wan_vlan_id:
                vlan = db.get(Vlan, ont.wan_vlan_id)
                wan_vlan_tag = int(vlan.tag) if vlan and vlan.tag is not None else None

            password_for_push = (
                pppoe_password.strip()
                if pppoe_password and pppoe_password.strip()
                else decrypt_credential(ont.pppoe_password)
                if getattr(ont, "pppoe_password", None)
                else ""
            )

            _persist_ont_plan_step(
                db,
                ont_id,
                "configure_wan_tr069",
                {
                    "wan_mode": "pppoe",
                    "wan_vlan_id": str(ont.wan_vlan_id) if ont.wan_vlan_id else None,
                    "wan_vlan": wan_vlan_tag,
                },
            )
            _persist_ont_plan_step(
                db,
                ont_id,
                "push_pppoe_omci",
                {
                    "vlan_id": wan_vlan_tag,
                    "username": ont.pppoe_username,
                    "password_set": bool(password_for_push),
                    "ip_index": 1,
                    "priority": 0,
                },
            )

            if not ont.pppoe_username:
                push_messages.append("PPPoE OMCI: username is required.")
                push_success = False
            if not password_for_push:
                push_messages.append("PPPoE OMCI: password is required.")
                push_success = False
            if wan_vlan_tag is None:
                push_messages.append("PPPoE OMCI: internet VLAN is required.")
                push_success = False

            if push_success:
                step_result = ont_provision_steps.push_pppoe_omci(
                    db,
                    ont_id,
                    vlan_id=wan_vlan_tag,
                    username=ont.pppoe_username,
                    password=password_for_push,
                )
                push_messages.append(f"PPPoE OMCI: {step_result.message}")
                if not step_result.success:
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
                    "push_success": push_success,
                    "config_method": config_method_value,
                },
            )
            message = "Configuration saved. " + "; ".join(push_messages)
            return ActionResult(success=push_success, message=message)

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

        wan_vlan_tag = None
        if ont.wan_vlan_id:
            vlan = db.get(Vlan, ont.wan_vlan_id)
            wan_vlan_tag = vlan.tag if vlan else None
        if ont.wan_mode:
            wan_mode_for_push = ont.wan_mode.value if ont.wan_mode else "dhcp"
            if wan_mode_for_push == "static_ip":
                wan_mode_for_push = "static"
            elif wan_mode_for_push == "setup_via_onu":
                wan_mode_for_push = "bridge"
            if wan_mode_for_push == "static":
                push_messages.append(
                    "WAN: static mode requires IP, subnet, gateway, and DNS. "
                    "Use the WAN / PPPoE unified form to push static WAN settings."
                )
                push_success = False
            else:
                result = configure_wan_config(
                    db,
                    ont_id,
                    wan_mode=wan_mode_for_push,
                    wan_vlan=wan_vlan_tag,
                    request=request,
                )
                push_messages.append(f"WAN: {result.message}")
                if not result.success:
                    push_success = False

        password_for_push = (
            pppoe_password.strip()
            if pppoe_password and pppoe_password.strip()
            else decrypt_credential(ont.pppoe_password)
            if getattr(ont, "pppoe_password", None)
            else ""
        )
        if (
            push_success
            and ont.wan_mode
            and ont.wan_mode.value == "pppoe"
            and ont.pppoe_username
            and password_for_push
        ):
            result = set_pppoe_credentials(
                db,
                ont_id,
                ont.pppoe_username,
                password_for_push,
                wan_vlan=int(wan_vlan_tag) if wan_vlan_tag is not None else None,
                request=request,
            )
            push_messages.append(f"PPPoE: {result.message}")
            if not result.success:
                push_success = False
        elif push_success and ont.wan_mode and ont.wan_mode.value == "pppoe":
            push_messages.append("PPPoE: password is required to push credentials.")
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
                security_mode=wifi_security_mode.strip() if wifi_security_mode else None,
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
