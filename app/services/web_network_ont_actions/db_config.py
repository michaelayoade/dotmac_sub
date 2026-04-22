"""Database configuration management for ONT web actions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import (
    OntConfigOverride,
    OntConfigOverrideSource,
    OntProvisioningProfile,
    Vlan,
)
from app.services import network as network_service
from app.services.network.ont_bundle_assignments import (
    assign_bundle_to_ont,
    clear_active_bundle_assignment,
    get_active_bundle_assignment,
)
from app.services.network.ont_config_overrides import (
    clear_bundle_managed_legacy_projection,
)
from app.services.network.effective_ont_config import resolve_effective_ont_config
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
    row = db.scalars(
        select(OntConfigOverride)
        .where(OntConfigOverride.ont_unit_id == ont.id)
        .where(OntConfigOverride.field_name == field_name)
        .limit(1)
    ).first()

    normalized = _normalize_override_value(value)
    if normalized is None:
        if row is not None:
            db.delete(row)
        return

    if row is None:
        row = OntConfigOverride(
            ont_unit_id=ont.id,
            field_name=field_name,
            source=OntConfigOverrideSource.operator,
        )
        db.add(row)
    row.value_json = {"value": normalized}
    row.reason = "configure_form"


def _persist_bundle_authoring_state(
    db: Session,
    *,
    ont,
    bundle_id: str | None,
    wan_mode: str | None,
    wan_vlan_tag: int | None,
    config_method: str | None,
    ip_protocol: str | None,
    pppoe_username: str | None,
    mgmt_ip_mode: str | None,
    mgmt_vlan_tag: int | None,
    mgmt_ip_address: str | None,
    wifi_enabled: bool,
    wifi_ssid: str | None,
    wifi_channel: str | None,
    wifi_security_mode: str | None,
) -> None:
    active_bundle = None
    if bundle_id == "":
        clear_active_bundle_assignment(db, ont=ont)
    elif bundle_id:
        active_bundle = db.get(OntProvisioningProfile, bundle_id)
        if active_bundle is not None:
            assign_bundle_to_ont(
                db,
                ont=ont,
                bundle=active_bundle,
                assigned_reason="configure_form",
            )

    if active_bundle is None:
        active_assignment = get_active_bundle_assignment(db, ont)
        active_bundle = getattr(active_assignment, "bundle", None)
        active_bundle_id = getattr(active_assignment, "bundle_id", None)
        if active_bundle is None and active_bundle_id is not None:
            active_bundle = db.get(OntProvisioningProfile, active_bundle_id)

    if active_bundle is None:
        for field_name in (
            "config_method",
            "ip_protocol",
            "wan.wan_mode",
            "wan.vlan_tag",
            "wan.pppoe_username",
            "management.ip_mode",
            "management.vlan_tag",
            "management.ip_address",
            "wifi.enabled",
            "wifi.ssid",
            "wifi.channel",
            "wifi.security_mode",
        ):
            _upsert_override(db, ont=ont, field_name=field_name, value=None)
        return

    active_services = [
        service
        for service in (getattr(active_bundle, "wan_services", None) or [])
        if getattr(service, "is_active", False)
    ]
    active_services.sort(
        key=lambda service: (
            getattr(service, "priority", 9999),
            getattr(service, "name", "") or "",
        )
    )
    primary_wan = active_services[0] if active_services else None

    override_pairs = {
        "config_method": (config_method, getattr(getattr(active_bundle, "config_method", None), "value", getattr(active_bundle, "config_method", None))),
        "ip_protocol": (ip_protocol, getattr(getattr(active_bundle, "ip_protocol", None), "value", getattr(active_bundle, "ip_protocol", None))),
        "wan.wan_mode": (
            wan_mode,
            getattr(getattr(primary_wan, "connection_type", None), "value", getattr(primary_wan, "connection_type", None)),
        ),
        "wan.vlan_tag": (wan_vlan_tag, getattr(primary_wan, "s_vlan", None)),
        "wan.pppoe_username": (
            pppoe_username,
            getattr(primary_wan, "pppoe_username_template", None),
        ),
        "management.ip_mode": (
            mgmt_ip_mode,
            getattr(getattr(active_bundle, "mgmt_ip_mode", None), "value", getattr(active_bundle, "mgmt_ip_mode", None)),
        ),
        "management.vlan_tag": (mgmt_vlan_tag, getattr(active_bundle, "mgmt_vlan_tag", None)),
        "management.ip_address": (mgmt_ip_address, None),
        "wifi.enabled": (wifi_enabled, getattr(active_bundle, "wifi_enabled", None)),
        "wifi.ssid": (wifi_ssid, getattr(active_bundle, "wifi_ssid_template", None)),
        "wifi.channel": (wifi_channel, getattr(active_bundle, "wifi_channel", None)),
        "wifi.security_mode": (
            wifi_security_mode,
            getattr(active_bundle, "wifi_security_mode", None),
        ),
    }

    for field_name, (submitted, bundle_value) in override_pairs.items():
        if _normalize_override_value(submitted) == _normalize_override_value(bundle_value):
            _upsert_override(db, ont=ont, field_name=field_name, value=None)
        else:
            _upsert_override(db, ont=ont, field_name=field_name, value=submitted)


def update_ont_config(
    db: Session,
    ont_id: str,
    *,
    bundle_id: str | None = None,
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
    active_assignment = get_active_bundle_assignment(db, ont)
    target_bundle_managed = bool(
        bundle_id not in (None, "")
        or (
            bundle_id is None
            and active_assignment is not None
        )
    )
    should_project_legacy_fields = not target_bundle_managed or push_to_device
    resolved_wan_vlan = None
    resolved_mgmt_vlan = None

    if should_project_legacy_fields and wan_mode:
        try:
            ont.wan_mode = WanMode(wan_mode)
        except ValueError:
            pass
    elif should_project_legacy_fields and wan_mode == "":
        ont.wan_mode = None

    if should_project_legacy_fields and config_method:
        try:
            ont.config_method = ConfigMethod(config_method)
        except ValueError:
            pass
    elif should_project_legacy_fields and config_method == "":
        ont.config_method = None

    if should_project_legacy_fields and ip_protocol:
        try:
            ont.ip_protocol = IpProtocol(ip_protocol)
        except ValueError:
            pass
    elif should_project_legacy_fields and ip_protocol == "":
        ont.ip_protocol = None

    if should_project_legacy_fields and mgmt_ip_mode:
        try:
            ont.mgmt_ip_mode = MgmtIpMode(mgmt_ip_mode)
        except ValueError:
            pass
    elif should_project_legacy_fields and mgmt_ip_mode == "":
        ont.mgmt_ip_mode = None

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
        if should_project_legacy_fields:
            ont.wan_vlan_id = vlan.id if vlan else None
    elif should_project_legacy_fields and wan_vlan_id == "":
        ont.wan_vlan_id = None

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
        if should_project_legacy_fields:
            ont.mgmt_vlan_id = vlan.id if vlan else None
    elif should_project_legacy_fields and mgmt_vlan_id == "":
        ont.mgmt_vlan_id = None

    if should_project_legacy_fields and pppoe_username is not None:
        ont.pppoe_username = pppoe_username.strip() or None
    if pppoe_password is not None and pppoe_password.strip():
        ont.pppoe_password = encrypt_credential(pppoe_password.strip())
    if should_project_legacy_fields and mgmt_ip_address is not None:
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

    if should_project_legacy_fields and wifi_ssid is not None:
        ont.wifi_ssid = wifi_ssid.strip() or None
    if should_project_legacy_fields and hasattr(ont, "wifi_enabled"):
        ont.wifi_enabled = wifi_enabled
    if should_project_legacy_fields and wifi_channel is not None and hasattr(ont, "wifi_channel"):
        ont.wifi_channel = wifi_channel.strip() or None
    if should_project_legacy_fields and wifi_security_mode is not None and hasattr(ont, "wifi_security_mode"):
        ont.wifi_security_mode = wifi_security_mode.strip() or None

    wan_vlan_tag = None
    if resolved_wan_vlan is not None:
        wan_vlan_tag = (
            int(resolved_wan_vlan.tag) if resolved_wan_vlan.tag is not None else None
        )
    elif ont.wan_vlan_id:
        vlan = db.get(Vlan, ont.wan_vlan_id)
        wan_vlan_tag = int(vlan.tag) if vlan and vlan.tag is not None else None

    mgmt_vlan_tag = None
    if resolved_mgmt_vlan is not None:
        mgmt_vlan_tag = (
            int(resolved_mgmt_vlan.tag) if resolved_mgmt_vlan.tag is not None else None
        )
    elif ont.mgmt_vlan_id:
        vlan = db.get(Vlan, ont.mgmt_vlan_id)
        mgmt_vlan_tag = int(vlan.tag) if vlan and vlan.tag is not None else None

    _persist_bundle_authoring_state(
        db,
        ont=ont,
        bundle_id=bundle_id,
        wan_mode=wan_mode,
        wan_vlan_tag=wan_vlan_tag,
        config_method=config_method,
        ip_protocol=ip_protocol,
        pppoe_username=pppoe_username.strip() if pppoe_username is not None else None,
        mgmt_ip_mode=mgmt_ip_mode,
        mgmt_vlan_tag=mgmt_vlan_tag,
        mgmt_ip_address=mgmt_ip_address.strip() if mgmt_ip_address is not None else None,
        wifi_enabled=wifi_enabled,
        wifi_ssid=wifi_ssid.strip() if wifi_ssid is not None else None,
        wifi_channel=wifi_channel,
        wifi_security_mode=wifi_security_mode,
    )
    if get_active_bundle_assignment(db, ont) is not None:
        clear_bundle_managed_legacy_projection(ont)

    db.add(ont)
    db.flush()

    push_messages: list[str] = []
    push_success = True

    if push_to_device:
        effective = resolve_effective_ont_config(db, ont)
        effective_values = (
            effective.get("values", {}) if isinstance(effective, dict) else {}
        )
        config_method_value = getattr(
            getattr(ont, "config_method", None), "value", None
        )
        wan_mode_value = str(effective_values.get("wan_mode") or "").strip() or None

        if config_method_value == "omci" and wan_mode_value == "pppoe":
            wan_vlan_tag = None
            if effective_values.get("wan_vlan") is not None:
                wan_vlan_tag = int(effective_values["wan_vlan"])
            elif ont.wan_vlan_id:
                vlan = db.get(Vlan, ont.wan_vlan_id)
                wan_vlan_tag = int(vlan.tag) if vlan and vlan.tag is not None else None

            pppoe_username_for_push = str(
                effective_values.get("pppoe_username") or ""
            ).strip() or None

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
                    "username": pppoe_username_for_push,
                    "password_set": bool(password_for_push),
                    "ip_index": 1,
                    "priority": 0,
                },
            )

            if not pppoe_username_for_push:
                push_messages.append("PPPoE OMCI: username is required.")
                push_success = False
            if not password_for_push:
                push_messages.append("PPPoE OMCI: password is required.")
                push_success = False
            if wan_vlan_tag is None:
                push_messages.append("PPPoE OMCI: internet VLAN is required.")
                push_success = False

            if (
                push_success
                and wan_vlan_tag is not None
                and pppoe_username_for_push
                and password_for_push
            ):
                step_result = ont_provision_steps.push_pppoe_omci(
                    db,
                    ont_id,
                    vlan_id=wan_vlan_tag,
                    username=pppoe_username_for_push,
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
        if effective_values.get("wan_vlan") is not None:
            wan_vlan_tag = effective_values.get("wan_vlan")
        elif ont.wan_vlan_id:
            vlan = db.get(Vlan, ont.wan_vlan_id)
            wan_vlan_tag = vlan.tag if vlan else None
        if wan_mode_value:
            wan_mode_for_push = wan_mode_value
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
            and wan_mode_value == "pppoe"
            and effective_values.get("pppoe_username")
            and password_for_push
        ):
            result = set_pppoe_credentials(
                db,
                ont_id,
                str(effective_values.get("pppoe_username")),
                password_for_push,
                wan_vlan=int(wan_vlan_tag) if wan_vlan_tag is not None else None,
                request=request,
            )
            push_messages.append(f"PPPoE: {result.message}")
            if not result.success:
                push_success = False
        elif push_success and wan_mode_value == "pppoe":
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
