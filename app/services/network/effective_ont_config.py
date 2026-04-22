"""Resolve ONT desired state as bundle + sparse overrides.

Mental model:
- Primary source is one assigned bundle
- Overrides are the explicit fields stored for this ONT
- Legacy OntUnit flat fields are consulted only when no bundle is assigned
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntConfigOverride,
    OntProfileWanService,
    OntUnit,
)
from app.services.network.ont_bundle_assignments import get_active_bundle_assignment

_CANONICAL_OVERRIDE_KEYS = {
    "config_method": ("config_method",),
    "onu_mode": ("onu_mode",),
    "ip_protocol": ("ip_protocol",),
    "wan_mode": ("wan.wan_mode", "wan_mode"),
    "wan_vlan": ("wan.vlan_tag", "wan_vlan", "wan_vlan_id"),
    "pppoe_username": ("wan.pppoe_username", "pppoe_username"),
    "mgmt_ip_mode": ("management.ip_mode", "mgmt_ip_mode"),
    "mgmt_vlan": ("management.vlan_tag", "mgmt_vlan", "mgmt_vlan_id"),
    "mgmt_ip_address": ("management.ip_address", "mgmt_ip_address"),
    "wifi_enabled": ("wifi.enabled", "wifi_enabled"),
    "wifi_ssid": ("wifi.ssid", "wifi_ssid"),
    "wifi_channel": ("wifi.channel", "wifi_channel"),
    "wifi_security_mode": ("wifi.security_mode", "wifi_security_mode"),
}


def _enum_or_raw(value: Any) -> Any:
    return getattr(value, "value", value)


def _coerce_override_value(raw: Any) -> Any:
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value")
    return raw


def _resolve_assigned_bundle(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> OntProvisioningProfile | None:
    assignment = get_active_bundle_assignment(db, ont)
    assigned_bundle = assignment.bundle if assignment is not None else None
    if assigned_bundle and assigned_bundle.is_active:
        return assigned_bundle

    return None


def _first_active_wan_service(
    profile: OntProvisioningProfile | None,
) -> OntProfileWanService | None:
    if profile is None:
        return None
    services = [
        service
        for service in (getattr(profile, "wan_services", None) or [])
        if getattr(service, "is_active", False)
    ]
    services.sort(
        key=lambda service: (
            getattr(service, "priority", 9999),
            getattr(service, "name", "") or "",
        )
    )
    return services[0] if services else None


def _load_raw_overrides(db: Session, ont: OntUnit) -> dict[str, Any]:
    rows = db.scalars(
        select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
    ).all()
    return {
        str(row.field_name): _coerce_override_value(getattr(row, "value_json", None))
        for row in rows
    }


def _canonicalize_overrides(raw_overrides: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for key, aliases in _CANONICAL_OVERRIDE_KEYS.items():
        for alias in aliases:
            if alias in raw_overrides and raw_overrides[alias] not in (None, ""):
                canonical[key] = raw_overrides[alias]
                break
    return canonical


def _bundle_values(bundle: OntProvisioningProfile | None) -> dict[str, Any]:
    primary_wan = _first_active_wan_service(bundle)
    return {
        "config_method": _enum_or_raw(getattr(bundle, "config_method", None)),
        "onu_mode": _enum_or_raw(getattr(bundle, "onu_mode", None)),
        "ip_protocol": _enum_or_raw(getattr(bundle, "ip_protocol", None)),
        "wan_mode": _enum_or_raw(getattr(primary_wan, "connection_type", None)),
        "wan_vlan": getattr(primary_wan, "s_vlan", None),
        "pppoe_username": getattr(primary_wan, "pppoe_username_template", None),
        "mgmt_ip_mode": _enum_or_raw(getattr(bundle, "mgmt_ip_mode", None)),
        "mgmt_vlan": getattr(bundle, "mgmt_vlan_tag", None),
        "mgmt_ip_address": None,
        "wifi_enabled": getattr(bundle, "wifi_enabled", None),
        "wifi_ssid": getattr(bundle, "wifi_ssid_template", None),
        "wifi_channel": getattr(bundle, "wifi_channel", None),
        "wifi_security_mode": getattr(bundle, "wifi_security_mode", None),
        "primary_wan_service": primary_wan,
    }


def _legacy_values(ont: OntUnit) -> dict[str, Any]:
    return {
        "config_method": _enum_or_raw(getattr(ont, "config_method", None)),
        "onu_mode": _enum_or_raw(getattr(ont, "onu_mode", None)),
        "ip_protocol": _enum_or_raw(getattr(ont, "ip_protocol", None)),
        "wan_mode": _enum_or_raw(getattr(ont, "wan_mode", None)),
        "wan_vlan": getattr(getattr(ont, "wan_vlan", None), "tag", None),
        "pppoe_username": getattr(ont, "pppoe_username", None),
        "mgmt_ip_mode": _enum_or_raw(getattr(ont, "mgmt_ip_mode", None)),
        "mgmt_vlan": getattr(getattr(ont, "mgmt_vlan", None), "tag", None),
        "mgmt_ip_address": getattr(ont, "mgmt_ip_address", None),
        "wifi_enabled": getattr(ont, "wifi_enabled", None),
        "wifi_ssid": getattr(ont, "wifi_ssid", None),
        "wifi_channel": getattr(ont, "wifi_channel", None),
        "wifi_security_mode": getattr(ont, "wifi_security_mode", None),
        "primary_wan_service": None,
    }


def resolve_effective_ont_config(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> dict[str, Any]:
    """Return effective ONT config as bundle + sparse overrides."""
    bundle = _resolve_assigned_bundle(db, ont, olt=olt)
    raw_overrides = _load_raw_overrides(db, ont)
    overrides = _canonicalize_overrides(raw_overrides)
    using_legacy_fallback = bundle is None

    values = _legacy_values(ont) if using_legacy_fallback else _bundle_values(bundle)
    for key, value in overrides.items():
        values[key] = value

    return {
        "bundle": bundle,
        "overrides": sorted(overrides.keys()),
        "values": values,
        "using_legacy_fallback": using_legacy_fallback,
    }
