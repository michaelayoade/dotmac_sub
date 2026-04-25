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
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_bundle_assignments import (
    assign_bundle_to_ont,
    clear_active_bundle_assignment,
    get_active_bundle_assignment,
)
from app.services.web_network_ont_actions._common import (
    _log_action_audit,
)
from app.services.web_network_ont_actions.config_setters import (
    set_lan_config,
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
        if active_bundle is not None and active_bundle.is_active:
            assign_bundle_to_ont(
                db,
                ont=ont,
                bundle=active_bundle,
                assigned_reason="configure_form",
            )
        elif active_bundle is not None:
            raise ValueError(f"Provisioning bundle '{active_bundle.name}' is inactive")

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

    # Update non-bundle-managed fields (still on ONT model)
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
