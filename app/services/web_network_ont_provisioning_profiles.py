"""Service helpers for admin ONT provisioning profile web routes."""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OntProfileType,
    OntProfileWanService,
    OntProvisioningProfile,
    OnuMode,
    PppoePasswordMode,
    VlanMode,
    WanConnectionType,
    WanServiceType,
)
from app.services.network.ont_provisioning_profiles import (
    ont_provisioning_profiles,
    wan_services,
)
from app.services.network.speed_profiles import speed_profiles

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _form_int(form: FormData, key: str, default: int | None = None) -> int | None:
    raw = _form_str(form, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _form_bool(form: FormData, key: str) -> bool:
    return _form_str(form, key) == "true"


def _get_org_id(request: Request) -> str:
    """Extract organization ID from the current admin session."""
    from app.web.admin import get_current_user

    user = get_current_user(request)
    return str(user.get("organization_id", ""))


def list_context(
    request: Request,
    db: Session,
    search: str | None = None,
    profile_type: str | None = None,
    config_method: str | None = None,
) -> dict[str, object]:
    """Return context dict for the provisioning profile list page."""
    from app.web.admin import get_current_user, get_sidebar_stats

    org_id = _get_org_id(request)
    items = ont_provisioning_profiles.list(
        db,
        organization_id=org_id,
        search=search,
        profile_type=profile_type,
        config_method=config_method,
        is_active=None,
    )
    return {
        "request": request,
        "active_page": "provisioning-profiles",
        "active_menu": "network",
        "items": items,
        "profile_types": [e.value for e in OntProfileType],
        "config_methods": [e.value for e in ConfigMethod],
        "search": search or "",
        "profile_type_filter": profile_type or "",
        "config_method_filter": config_method or "",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def form_context(
    request: Request,
    db: Session,
    profile_id: str | None = None,
) -> dict[str, object]:
    """Return context dict for the create/edit form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    item = ont_provisioning_profiles.get(db, profile_id) if profile_id else None

    # Load WAN services for this profile
    profile_wan_services: list[OntProfileWanService] = []
    if item:
        profile_wan_services = wan_services.list_for_profile(db, str(item.id))

    # Load speed profiles for dropdowns
    dl_profiles = speed_profiles.list(db, direction="download", is_active=True)
    ul_profiles = speed_profiles.list(db, direction="upload", is_active=True)

    return {
        "request": request,
        "active_page": "provisioning-profiles",
        "active_menu": "network",
        "item": item,
        "wan_services": profile_wan_services,
        # Enum choices
        "profile_types": [e.value for e in OntProfileType],
        "config_methods": [e.value for e in ConfigMethod],
        "onu_modes": [e.value for e in OnuMode],
        "ip_protocols": [e.value for e in IpProtocol],
        "mgmt_ip_modes": [e.value for e in MgmtIpMode],
        "wan_service_types": [e.value for e in WanServiceType],
        "vlan_modes": [e.value for e in VlanMode],
        "wan_connection_types": [e.value for e in WanConnectionType],
        "pppoe_password_modes": [e.value for e in PppoePasswordMode],
        # FK choices
        "download_speed_profiles": dl_profiles,
        "upload_speed_profiles": ul_profiles,
        "action_url": (
            f"/admin/network/provisioning-profiles/{profile_id}/edit"
            if profile_id
            else "/admin/network/provisioning-profiles/create"
        ),
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def parse_profile_form(form: FormData) -> dict[str, object]:
    """Parse profile form fields into normalized values."""
    return {
        "name": _form_str(form, "name"),
        "profile_type": _form_str(form, "profile_type"),
        "description": _form_str(form, "description") or None,
        "config_method": _form_str(form, "config_method") or None,
        "onu_mode": _form_str(form, "onu_mode") or None,
        "ip_protocol": _form_str(form, "ip_protocol") or None,
        "download_speed_profile_id": _form_str(form, "download_speed_profile_id") or None,
        "upload_speed_profile_id": _form_str(form, "upload_speed_profile_id") or None,
        "mgmt_ip_mode": _form_str(form, "mgmt_ip_mode") or None,
        "mgmt_vlan_tag": _form_int(form, "mgmt_vlan_tag"),
        "mgmt_remote_access": _form_bool(form, "mgmt_remote_access"),
        "wifi_enabled": _form_bool(form, "wifi_enabled"),
        "wifi_ssid_template": _form_str(form, "wifi_ssid_template") or None,
        "wifi_security_mode": _form_str(form, "wifi_security_mode") or None,
        "wifi_channel": _form_str(form, "wifi_channel") or None,
        "wifi_band": _form_str(form, "wifi_band") or None,
        "voip_enabled": _form_bool(form, "voip_enabled"),
        "is_default": _form_bool(form, "is_default"),
        "notes": _form_str(form, "notes") or None,
    }


def validate_profile_form(values: dict[str, object]) -> str | None:
    """Validate profile form values. Returns error message or None."""
    name = values.get("name")
    if not name or not str(name).strip():
        return "Profile name is required."
    pt = values.get("profile_type")
    if not pt:
        return "Profile type is required."
    try:
        OntProfileType(str(pt))
    except ValueError:
        return f"Invalid profile type: {pt}"
    # Validate optional enums if provided
    cm = values.get("config_method")
    if cm:
        try:
            ConfigMethod(str(cm))
        except ValueError:
            return f"Invalid config method: {cm}"
    om = values.get("onu_mode")
    if om:
        try:
            OnuMode(str(om))
        except ValueError:
            return f"Invalid ONU mode: {om}"
    return None


def handle_create(
    request: Request, db: Session, form_data: dict[str, object]
) -> OntProvisioningProfile:
    """Create a new provisioning profile from validated form values."""
    org_id = _get_org_id(request)
    return ont_provisioning_profiles.create(
        db,
        organization_id=org_id,
        name=str(form_data["name"]),
        profile_type=OntProfileType(str(form_data["profile_type"])),
        description=str(form_data["description"]) if form_data.get("description") else None,
        config_method=ConfigMethod(str(form_data["config_method"])) if form_data.get("config_method") else None,
        onu_mode=OnuMode(str(form_data["onu_mode"])) if form_data.get("onu_mode") else None,
        ip_protocol=IpProtocol(str(form_data["ip_protocol"])) if form_data.get("ip_protocol") else None,
        download_speed_profile_id=str(form_data["download_speed_profile_id"]) if form_data.get("download_speed_profile_id") else None,
        upload_speed_profile_id=str(form_data["upload_speed_profile_id"]) if form_data.get("upload_speed_profile_id") else None,
        mgmt_ip_mode=MgmtIpMode(str(form_data["mgmt_ip_mode"])) if form_data.get("mgmt_ip_mode") else None,
        mgmt_vlan_tag=int(str(form_data["mgmt_vlan_tag"])) if form_data.get("mgmt_vlan_tag") is not None else None,
        mgmt_remote_access=bool(form_data.get("mgmt_remote_access")),
        wifi_enabled=bool(form_data.get("wifi_enabled")),
        wifi_ssid_template=str(form_data["wifi_ssid_template"]) if form_data.get("wifi_ssid_template") else None,
        wifi_security_mode=str(form_data["wifi_security_mode"]) if form_data.get("wifi_security_mode") else None,
        wifi_channel=str(form_data["wifi_channel"]) if form_data.get("wifi_channel") else None,
        wifi_band=str(form_data["wifi_band"]) if form_data.get("wifi_band") else None,
        voip_enabled=bool(form_data.get("voip_enabled")),
        is_default=bool(form_data.get("is_default")),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def handle_update(
    request: Request,
    db: Session,
    profile_id: str,
    form_data: dict[str, object],
) -> OntProvisioningProfile:
    """Update a provisioning profile from validated form values."""
    return ont_provisioning_profiles.update(
        db,
        profile_id,
        name=str(form_data["name"]),
        profile_type=OntProfileType(str(form_data["profile_type"])),
        description=str(form_data["description"]) if form_data.get("description") else None,
        config_method=ConfigMethod(str(form_data["config_method"])) if form_data.get("config_method") else None,
        onu_mode=OnuMode(str(form_data["onu_mode"])) if form_data.get("onu_mode") else None,
        ip_protocol=IpProtocol(str(form_data["ip_protocol"])) if form_data.get("ip_protocol") else None,
        download_speed_profile_id=str(form_data["download_speed_profile_id"]) if form_data.get("download_speed_profile_id") else None,
        upload_speed_profile_id=str(form_data["upload_speed_profile_id"]) if form_data.get("upload_speed_profile_id") else None,
        mgmt_ip_mode=MgmtIpMode(str(form_data["mgmt_ip_mode"])) if form_data.get("mgmt_ip_mode") else None,
        mgmt_vlan_tag=int(str(form_data["mgmt_vlan_tag"])) if form_data.get("mgmt_vlan_tag") is not None else None,
        mgmt_remote_access=bool(form_data.get("mgmt_remote_access")),
        wifi_enabled=bool(form_data.get("wifi_enabled")),
        wifi_ssid_template=str(form_data["wifi_ssid_template"]) if form_data.get("wifi_ssid_template") else None,
        wifi_security_mode=str(form_data["wifi_security_mode"]) if form_data.get("wifi_security_mode") else None,
        wifi_channel=str(form_data["wifi_channel"]) if form_data.get("wifi_channel") else None,
        wifi_band=str(form_data["wifi_band"]) if form_data.get("wifi_band") else None,
        voip_enabled=bool(form_data.get("voip_enabled")),
        is_default=bool(form_data.get("is_default")),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def parse_wan_service_form(form: FormData) -> dict[str, object]:
    """Parse WAN service form fields."""
    return {
        "service_type": _form_str(form, "service_type"),
        "name": _form_str(form, "wan_service_name") or None,
        "priority": _form_int(form, "priority", 1),
        "vlan_mode": _form_str(form, "vlan_mode"),
        "s_vlan": _form_int(form, "s_vlan"),
        "c_vlan": _form_int(form, "c_vlan"),
        "cos_priority": _form_int(form, "cos_priority"),
        "mtu": _form_int(form, "mtu", 1500),
        "connection_type": _form_str(form, "connection_type"),
        "nat_enabled": _form_bool(form, "nat_enabled"),
        "ip_mode": _form_str(form, "wan_ip_mode") or None,
        "pppoe_username_template": _form_str(form, "pppoe_username_template") or None,
        "pppoe_password_mode": _form_str(form, "pppoe_password_mode") or None,
        "pppoe_static_password": _form_str(form, "pppoe_static_password") or None,
        "static_ip_source": _form_str(form, "static_ip_source") or None,
        "gem_port_id": _form_int(form, "gem_port_id"),
        "t_cont_profile": _form_str(form, "t_cont_profile") or None,
        "notes": _form_str(form, "wan_notes") or None,
    }


def validate_wan_service_form(values: dict[str, object]) -> str | None:
    """Validate WAN service form values."""
    st = values.get("service_type")
    if not st:
        return "Service type is required."
    try:
        WanServiceType(str(st))
    except ValueError:
        return f"Invalid service type: {st}"
    ct = values.get("connection_type")
    if not ct:
        return "Connection type is required."
    try:
        WanConnectionType(str(ct))
    except ValueError:
        return f"Invalid connection type: {ct}"
    vm = values.get("vlan_mode")
    if not vm:
        return "VLAN mode is required."
    try:
        VlanMode(str(vm))
    except ValueError:
        return f"Invalid VLAN mode: {vm}"
    return None


def handle_wan_service_create(
    db: Session, profile_id: str, form_data: dict[str, object]
) -> OntProfileWanService:
    """Create a WAN service from validated form values."""
    return wan_services.create(
        db,
        profile_id=profile_id,
        service_type=WanServiceType(str(form_data["service_type"])),
        name=str(form_data["name"]) if form_data.get("name") else None,
        priority=int(str(form_data.get("priority") or 1)),
        vlan_mode=VlanMode(str(form_data["vlan_mode"])),
        s_vlan=int(str(form_data["s_vlan"])) if form_data.get("s_vlan") is not None else None,
        c_vlan=int(str(form_data["c_vlan"])) if form_data.get("c_vlan") is not None else None,
        cos_priority=int(str(form_data["cos_priority"])) if form_data.get("cos_priority") is not None else None,
        mtu=int(str(form_data.get("mtu") or 1500)),
        connection_type=WanConnectionType(str(form_data["connection_type"])),
        nat_enabled=bool(form_data.get("nat_enabled")),
        ip_mode=IpProtocol(str(form_data["ip_mode"])) if form_data.get("ip_mode") else None,
        pppoe_username_template=str(form_data["pppoe_username_template"]) if form_data.get("pppoe_username_template") else None,
        pppoe_password_mode=PppoePasswordMode(str(form_data["pppoe_password_mode"])) if form_data.get("pppoe_password_mode") else None,
        pppoe_static_password=str(form_data["pppoe_static_password"]) if form_data.get("pppoe_static_password") else None,
        static_ip_source=str(form_data["static_ip_source"]) if form_data.get("static_ip_source") else None,
        gem_port_id=int(str(form_data["gem_port_id"])) if form_data.get("gem_port_id") is not None else None,
        t_cont_profile=str(form_data["t_cont_profile"]) if form_data.get("t_cont_profile") else None,
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )
