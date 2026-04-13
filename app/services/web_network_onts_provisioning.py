"""Route-facing service helpers for ONT provisioning web actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from ipaddress import ip_address

from sqlalchemy.orm import Session

from app.models.network import OntProvisioningProfile, OnuMode, Vlan, WanMode
from app.schemas.network import OntUnitUpdate
from app.schemas.provisioning import ServiceOrderUpdate
from app.services import network as network_service
from app.services import provisioning as provisioning_service
from app.services import web_network_olt_profiles as web_network_olt_profiles_service
from app.services import web_network_onts as web_network_onts_service
from app.services.common import coerce_uuid
from app.services.credential_crypto import encrypt_credential
from app.services.network.ont_provisioning.preflight import validate_prerequisites
from app.services.network.ont_provisioning.result import StepResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JsonActionResult:
    content: dict[str, object]
    status_code: int = 200


def profile_preview_context(
    db: Session,
    *,
    profile_id: str,
) -> dict[str, object] | None:
    """Return provisioning profile preview context, or None when not found."""
    pid = coerce_uuid(profile_id)
    if pid is None:
        return None
    profile = db.get(OntProvisioningProfile, str(pid))
    if profile is None:
        return None
    return {
        "profile": profile,
        "wan_services": list(profile.wan_services),
    }


def _effective_profile_ids(
    db: Session,
    *,
    ont_id: str,
    profile_id: str | None,
    tr069_profile_id: int | None,
) -> tuple[str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    olt = getattr(ont, "olt_device", None)
    resolved_profile = web_network_onts_service.resolve_effective_provisioning_profile(
        db, ont, olt
    )
    resolved_tr069_profile, _resolved_tr069_profile_error = (
        web_network_onts_service.resolve_effective_tr069_profile_for_ont(db, ont)
    )
    effective_profile_id = profile_id or (
        str(resolved_profile.id) if resolved_profile else None
    )
    effective_tr069_profile_id = tr069_profile_id or getattr(
        resolved_tr069_profile,
        "profile_id",
        None,
    )
    return effective_profile_id, effective_tr069_profile_id


def provisioning_preview_context(
    db: Session,
    *,
    ont_id: str,
    profile_id: str | None,
    tr069_profile_id: int | None,
) -> dict[str, object]:
    """Return command-preview context using explicit or effective profiles."""
    effective_profile_id, effective_tr069_profile_id = _effective_profile_ids(
        db,
        ont_id=ont_id,
        profile_id=profile_id,
        tr069_profile_id=tr069_profile_id,
    )
    return web_network_olt_profiles_service.command_preview_context(
        db,
        ont_id,
        effective_profile_id or "",
        tr069_olt_profile_id=effective_tr069_profile_id,
    )


def preflight_result(
    db: Session,
    *,
    ont_id: str,
    profile_id: str | None,
    tr069_profile_id: int | None,
) -> dict[str, object]:
    """Run provisioning preflight using explicit or effective profiles."""
    effective_profile_id, effective_tr069_profile_id = _effective_profile_ids(
        db,
        ont_id=ont_id,
        profile_id=profile_id,
        tr069_profile_id=tr069_profile_id,
    )
    return validate_prerequisites(
        db,
        ont_id,
        profile_id=effective_profile_id,
        tr069_olt_profile_id=effective_tr069_profile_id,
    )


def save_provision_settings(
    db: Session,
    *,
    ont_id: str,
    profile_id: str | None,
    tr069_profile_id: str | None,
    onu_mode: str | None,
    mgmt_vlan_id: str | None,
    mgmt_ip_mode: str | None,
    mgmt_ip_address: str | None,
    mgmt_subnet: str | None,
    mgmt_gateway: str | None,
    wan_protocol: str | None,
    wan_vlan_id: str | None,
    ip_pool_id: str | None,
    static_ip_pool_id: str | None,
    static_ip: str | None,
    static_subnet: str | None,
    static_gateway: str | None,
    static_dns: str | None,
    lan_ip: str | None,
    lan_subnet: str | None,
    dhcp_enabled: str | None,
    dhcp_start: str | None,
    dhcp_end: str | None,
    wifi_enabled: str | None,
    wifi_ssid: str | None,
    wifi_password: str | None,
    wifi_security_mode: str | None,
    wifi_channel: str | None,
    pppoe_username: str | None,
    pppoe_password: str | None,
) -> JsonActionResult:
    """Persist provision-page WAN settings without starting provisioning."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except Exception:
        return JsonActionResult(
            status_code=404,
            content={"success": False, "message": "ONT not found"},
        )

    onu_mode_value = (onu_mode or "").strip().lower() or None
    profile_id_value = (profile_id or "").strip() or None
    tr069_profile_id_value = (tr069_profile_id or "").strip() or None
    mgmt_vlan_id_value = (mgmt_vlan_id or "").strip() or None
    mgmt_ip_mode_value = (mgmt_ip_mode or "").strip().lower() or None
    mgmt_ip_address_value = (mgmt_ip_address or "").strip() or None
    mgmt_subnet_value = (mgmt_subnet or "").strip() or None
    mgmt_gateway_value = (mgmt_gateway or "").strip() or None
    wan_protocol_value = (wan_protocol or "").strip().lower() or None
    pppoe_username_value = (pppoe_username or "").strip() or None
    pppoe_password_value = (pppoe_password or "").strip() or None
    wan_vlan_id_value = (wan_vlan_id or "").strip() or None
    ip_pool_id_value = (ip_pool_id or "").strip() or None
    static_ip_pool_id_value = (static_ip_pool_id or "").strip() or None
    static_ip_value = (static_ip or "").strip() or None
    static_subnet_value = (static_subnet or "").strip() or None
    static_gateway_value = (static_gateway or "").strip() or None
    static_dns_value = (static_dns or "").strip() or None
    lan_ip_value = (lan_ip or "").strip() or None
    lan_subnet_value = (lan_subnet or "").strip() or None
    dhcp_enabled_value = _bool_from_form(dhcp_enabled)
    dhcp_start_value = (dhcp_start or "").strip() or None
    dhcp_end_value = (dhcp_end or "").strip() or None
    wifi_enabled_value = _bool_from_form(wifi_enabled)
    wifi_ssid_value = (wifi_ssid or "").strip() or None
    wifi_password_value = (wifi_password or "").strip() or None
    wifi_security_mode_value = (wifi_security_mode or "").strip() or None
    wifi_channel_value = (wifi_channel or "").strip() or None
    mgmt_vlan_tag_value = _vlan_tag_for_id(db, mgmt_vlan_id_value)
    wan_vlan_tag_value = _vlan_tag_for_id(db, wan_vlan_id_value)

    field_issues = validate_provision_form_fields(
        profile_id=profile_id_value,
        onu_mode=onu_mode_value,
        mgmt_vlan_id=mgmt_vlan_id_value,
        mgmt_ip_mode=mgmt_ip_mode_value,
        mgmt_ip_address=mgmt_ip_address_value,
        mgmt_subnet=mgmt_subnet_value,
        mgmt_gateway=mgmt_gateway_value,
        wan_protocol=wan_protocol_value,
        wan_vlan_id=wan_vlan_id_value,
        pppoe_username=pppoe_username_value,
        static_ip_pool_id=static_ip_pool_id_value,
        static_ip=static_ip_value,
        static_subnet=static_subnet_value,
        static_gateway=static_gateway_value,
        static_dns=static_dns_value,
        lan_ip=lan_ip_value,
        lan_subnet=lan_subnet_value,
        dhcp_enabled=dhcp_enabled_value,
        dhcp_start=dhcp_start_value,
        dhcp_end=dhcp_end_value,
        wifi_enabled=wifi_enabled_value,
        wifi_ssid=wifi_ssid_value,
        wifi_password=wifi_password_value,
    )
    if field_issues:
        return JsonActionResult(
            status_code=422,
            content={
                "success": False,
                "message": "Provisioning configuration is incomplete: "
                + "; ".join(field_issues),
                "issues": field_issues,
            },
        )

    if onu_mode_value not in {None, OnuMode.routing.value, OnuMode.bridging.value}:
        return JsonActionResult(
            status_code=422,
            content={"success": False, "message": "Invalid ONU mode"},
        )

    if mgmt_ip_mode_value == "static":
        mgmt_ip_mode_value = "static_ip"
    if mgmt_ip_mode_value not in {None, "inactive", "dhcp", "static_ip"}:
        return JsonActionResult(
            status_code=422,
            content={"success": False, "message": "Invalid management IP mode"},
        )
    if mgmt_ip_mode_value == "static_ip" and not (
        mgmt_ip_address_value and mgmt_subnet_value and mgmt_gateway_value
    ):
        return JsonActionResult(
            status_code=422,
            content={
                "success": False,
                "message": "Static management IP requires address, subnet, and gateway",
            },
        )

    wan_mode_value: str | None = None
    if onu_mode_value == OnuMode.bridging.value:
        wan_mode_value = "bridge"
    elif wan_protocol_value == "pppoe":
        wan_mode_value = WanMode.pppoe.value
    elif wan_protocol_value == "dhcp":
        wan_mode_value = WanMode.dhcp.value
    elif wan_protocol_value == "static":
        wan_mode_value = WanMode.static_ip.value
    elif wan_protocol_value:
        return JsonActionResult(
            status_code=422,
            content={"success": False, "message": "Invalid WAN protocol"},
        )
    if wan_protocol_value == "static" and not (
        static_ip_value or static_ip_pool_id_value
    ):
        return JsonActionResult(
            status_code=422,
            content={
                "success": False,
                "message": "Static internet deployment requires an IP address or pool",
            },
        )

    payload = OntUnitUpdate(
        onu_mode=onu_mode_value,
        mgmt_vlan_id=coerce_uuid(mgmt_vlan_id_value),
        mgmt_ip_mode=mgmt_ip_mode_value,
        mgmt_ip_address=mgmt_ip_address_value
        if mgmt_ip_mode_value == "static_ip"
        else None,
        wan_mode=wan_mode_value,
        wan_vlan_id=coerce_uuid(wan_vlan_id_value),
        pppoe_username=pppoe_username_value if wan_protocol_value == "pppoe" else None,
        pppoe_password=encrypt_credential(pppoe_password_value)
        if wan_protocol_value == "pppoe" and pppoe_password_value
        else None,
    )
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    profile_uuid = coerce_uuid(profile_id_value)
    if profile_uuid is not None:
        ont.provisioning_profile_id = profile_uuid
        db.commit()

    if mgmt_vlan_id_value or mgmt_ip_mode_value:
        update_service_order_execution_context_for_ont(
            db,
            ont_id=ont_id,
            step_name="configure_management_ip",
            values={
                "vlan_id": mgmt_vlan_tag_value,
                "mgmt_vlan_id": mgmt_vlan_id_value,
                "vlan_tag": mgmt_vlan_tag_value,
                "ip_mode": "static"
                if mgmt_ip_mode_value == "static_ip"
                else mgmt_ip_mode_value,
                "ip_address": mgmt_ip_address_value,
                "subnet": mgmt_subnet_value,
                "gateway": mgmt_gateway_value,
            },
        )
    if wan_vlan_id_value or wan_protocol_value:
        wan_values = {
            "wan_mode": wan_protocol_value,
            "wan_vlan_id": wan_vlan_id_value,
            "wan_vlan": wan_vlan_tag_value,
            "ip_pool_id": ip_pool_id_value,
            "static_ip_pool_id": static_ip_pool_id_value,
            "ip_address": static_ip_value,
            "subnet_mask": static_subnet_value,
            "gateway": static_gateway_value,
            "dns_servers": static_dns_value,
        }
        update_service_order_execution_context_for_ont(
            db,
            ont_id=ont_id,
            step_name="configure_wan_tr069",
            values=wan_values,
        )
        if tr069_profile_id_value:
            update_service_order_execution_context_for_ont(
                db,
                ont_id=ont_id,
                step_name="bind_tr069",
                values={"tr069_olt_profile_id": tr069_profile_id_value},
            )
        if wan_protocol_value == "pppoe":
            update_service_order_execution_context_for_ont(
                db,
                ont_id=ont_id,
                step_name="push_pppoe_tr069",
                values={"username": pppoe_username_value},
            )
    if any(
        value is not None
        for value in [
            lan_ip_value,
            lan_subnet_value,
            dhcp_enabled_value,
            dhcp_start_value,
            dhcp_end_value,
        ]
    ):
        update_service_order_execution_context_for_ont(
            db,
            ont_id=ont_id,
            step_name="configure_lan_tr069",
            values={
                "lan_ip": lan_ip_value,
                "lan_subnet": lan_subnet_value,
                "dhcp_enabled": dhcp_enabled_value,
                "dhcp_start": dhcp_start_value,
                "dhcp_end": dhcp_end_value,
            },
        )
    if any(
        value is not None
        for value in [
            wifi_enabled_value,
            wifi_ssid_value,
            wifi_password_value,
            wifi_security_mode_value,
            wifi_channel_value,
        ]
    ):
        update_service_order_execution_context_for_ont(
            db,
            ont_id=ont_id,
            step_name="configure_wifi_tr069",
            values={
                "enabled": wifi_enabled_value,
                "ssid": wifi_ssid_value,
                "password_set": bool(wifi_password_value),
                "security_mode": wifi_security_mode_value,
                "channel": wifi_channel_value,
            },
        )
    return JsonActionResult(
        content={"success": True, "message": "Provision settings saved"}
    )


def validate_provision_form_fields(
    *,
    profile_id: str | None,
    onu_mode: str | None,
    mgmt_vlan_id: str | None,
    mgmt_ip_mode: str | None,
    mgmt_ip_address: str | None,
    mgmt_subnet: str | None,
    mgmt_gateway: str | None,
    wan_protocol: str | None,
    wan_vlan_id: str | None,
    pppoe_username: str | None,
    static_ip_pool_id: str | None,
    static_ip: str | None,
    static_subnet: str | None,
    static_gateway: str | None,
    static_dns: str | None,
    lan_ip: str | None,
    lan_subnet: str | None,
    dhcp_enabled: bool | None,
    dhcp_start: str | None,
    dhcp_end: str | None,
    wifi_enabled: bool | None,
    wifi_ssid: str | None,
    wifi_password: str | None,
) -> list[str]:
    """Validate operator-selected provisioning inputs before allowing proceed."""
    issues: list[str] = []
    onu_mode_value = (onu_mode or "").strip().lower()
    mgmt_ip_mode_value = (mgmt_ip_mode or "").strip().lower()
    wan_protocol_value = (wan_protocol or "").strip().lower()

    if not profile_id:
        issues.append("Select service profile")
    if onu_mode_value not in {OnuMode.routing.value, OnuMode.bridging.value}:
        issues.append("Select ONU mode")
    if not mgmt_vlan_id:
        issues.append("Select management VLAN")
    if mgmt_ip_mode_value not in {"dhcp", "static", "static_ip"}:
        issues.append("Select management IP method")
    if mgmt_ip_mode_value in {"static", "static_ip"}:
        _require_ip(issues, "Management IP", mgmt_ip_address)
        _require_ip(issues, "Management subnet", mgmt_subnet)
        _require_ip(issues, "Management gateway", mgmt_gateway)

    if not wan_vlan_id:
        issues.append("Select internet VLAN")
    if onu_mode_value == OnuMode.bridging.value:
        if wan_protocol_value not in {"bridged", "bridge"}:
            issues.append("Bridge ONU mode requires bridged WAN protocol")
    elif wan_protocol_value not in {"pppoe", "dhcp", "static"}:
        issues.append("Select internet deployment method")

    if wan_protocol_value == "pppoe" and not pppoe_username:
        issues.append("Enter PPPoE username")
    if wan_protocol_value == "static":
        if not static_ip_pool_id:
            _require_ip(issues, "Static IP address", static_ip)
        _require_ip(issues, "Static subnet", static_subnet)
        _require_ip(issues, "Static gateway", static_gateway)
        if static_dns:
            for dns in [item.strip() for item in static_dns.split(",") if item.strip()]:
                _require_ip(issues, f"DNS server {dns}", dns)

    if onu_mode_value == OnuMode.routing.value:
        _require_ip(issues, "LAN gateway IP", lan_ip)
        _require_ip(issues, "LAN subnet", lan_subnet)
        if dhcp_enabled is True:
            _require_ip(issues, "DHCP start", dhcp_start)
            _require_ip(issues, "DHCP end", dhcp_end)

    if wifi_enabled is True:
        if not wifi_ssid:
            issues.append("Enter WiFi SSID")
        if wifi_password and len(wifi_password) < 8:
            issues.append("WiFi password must be at least 8 characters")

    return issues


def _require_ip(issues: list[str], label: str, value: str | None) -> None:
    if not value:
        issues.append(f"{label} is required")
        return
    try:
        ip_address(value)
    except ValueError:
        issues.append(f"{label} is invalid")


def _bool_from_form(value: str | None) -> bool | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    return raw in {"true", "1", "yes", "on", "enabled"}


def _vlan_tag_for_id(db: Session, vlan_id: str | None) -> int | None:
    vlan_uuid = coerce_uuid(vlan_id)
    if vlan_uuid is None:
        return None
    vlan = db.get(Vlan, vlan_uuid)
    return int(vlan.tag) if vlan and vlan.tag is not None else None


def update_service_order_execution_context_for_ont(
    db: Session,
    *,
    ont_id: str,
    step_name: str,
    values: dict[str, object],
) -> None:
    """Persist operator-selected step inputs onto the linked service order."""
    service_order_id = provisioning_service.resolve_service_order_id_for_ont(db, ont_id)
    if not service_order_id:
        return
    order = provisioning_service.service_orders.get(db, service_order_id)
    execution_context = dict(getattr(order, "execution_context", None) or {})
    ont_plan = dict(execution_context.get("ont_plan") or {})
    ont_plan[step_name] = {
        key: value for key, value in values.items() if value not in (None, "", [])
    }
    execution_context["ont_plan"] = ont_plan
    provisioning_service.service_orders.update(
        db,
        service_order_id,
        ServiceOrderUpdate(execution_context=execution_context),
    )


def record_ont_step_action(
    db: Session,
    *,
    ont_id: str,
    result: StepResult,
) -> None:
    """Record an operator-triggered ONT provisioning step."""
    logger.info(
        "ONT step %s for %s: success=%s waiting=%s - %s",
        result.step_name,
        ont_id,
        result.success,
        result.waiting,
        result.message,
    )
