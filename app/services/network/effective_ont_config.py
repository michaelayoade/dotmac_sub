"""Resolve ONT desired state as bundle + sparse overrides.

Mental model:
- Primary source is one active, applied bundle assignment.
- Overrides are explicit ONT-level fields that win over bundle values.
- Legacy OntUnit flat fields are consulted only when no bundle is assigned.
- Active assignments that are not applied, or point to inactive bundles, block
  legacy fallback so broken bundle state is not silently ignored.

TODO: Retire legacy OntUnit desired-state fallback after bundle backfill is complete.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntProfileWanService,
    OntProvisioningProfile,
    OntUnit,
)
from app.services.network.ont_bundle_assignments import get_active_bundle_assignment

logger = logging.getLogger(__name__)

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


def _assignment_blocked_reason(assignment: OntBundleAssignment | None) -> str | None:
    if assignment is None:
        return None
    if assignment.status != OntBundleAssignmentStatus.applied:
        return f"assignment_status_{_enum_or_raw(assignment.status)}"
    assigned_bundle = assignment.bundle
    if assigned_bundle is None:
        return "missing_bundle"
    if not assigned_bundle.is_active:
        return "inactive_bundle"
    return None


def _resolve_ready_bundle(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> tuple[OntBundleAssignment | None, OntProvisioningProfile | None, str | None]:
    assignment = get_active_bundle_assignment(db, ont)
    blocked_reason = _assignment_blocked_reason(assignment)
    if blocked_reason is not None:
        logger.warning(
            "ONT %s active bundle assignment is not config-ready: %s",
            getattr(ont, "serial_number", None) or getattr(ont, "id", None),
            blocked_reason,
        )
        return assignment, None, blocked_reason

    if assignment is not None:
        return assignment, assignment.bundle, None

    return None, None, None


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


def _resolve_template(template: str | None, context: dict[str, str]) -> str | None:
    if not template:
        return None
    result = template
    for key in (
        "subscriber_code",
        "subscriber_name",
        "serial_number",
        "offer_name",
        "ont_id_short",
    ):
        result = result.replace(f"{{{key}}}", context.get(key, ""))
    return result


def _subscriber_template_context(db: Session, ont: OntUnit) -> dict[str, str]:
    context = {
        "subscriber_code": "",
        "subscriber_name": "",
        "serial_number": getattr(ont, "serial_number", None) or "",
        "offer_name": "",
        "ont_id_short": str(ont.id)[:8] if getattr(ont, "id", None) else "",
    }
    active_assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()
    if not active_assignment or not active_assignment.subscriber_id:
        return context

    try:
        from app.services.network_subscriber_bridge import default_subscriber_validator

        context.update(
            default_subscriber_validator.get_template_context(
                db,
                subscriber_id=active_assignment.subscriber_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 - template context must not abort reads
        logger.warning(
            "Could not resolve subscriber template context for ONT %s: %s",
            getattr(ont, "serial_number", None) or getattr(ont, "id", None),
            exc,
        )
    return context


def _bundle_values(
    bundle: OntProvisioningProfile | None,
    *,
    template_context: dict[str, str],
) -> dict[str, Any]:
    primary_wan = _first_active_wan_service(bundle)
    return {
        "config_method": _enum_or_raw(getattr(bundle, "config_method", None)),
        "onu_mode": _enum_or_raw(getattr(bundle, "onu_mode", None)),
        "ip_protocol": _enum_or_raw(getattr(bundle, "ip_protocol", None)),
        "wan_mode": _enum_or_raw(getattr(primary_wan, "connection_type", None)),
        "wan_vlan": getattr(primary_wan, "s_vlan", None),
        "pppoe_username": _resolve_template(
            getattr(primary_wan, "pppoe_username_template", None),
            template_context,
        ),
        "mgmt_ip_mode": _enum_or_raw(getattr(bundle, "mgmt_ip_mode", None)),
        "mgmt_vlan": getattr(bundle, "mgmt_vlan_tag", None),
        "mgmt_ip_address": None,
        "wifi_enabled": getattr(bundle, "wifi_enabled", None),
        "wifi_ssid": _resolve_template(
            getattr(bundle, "wifi_ssid_template", None),
            template_context,
        ),
        "wifi_channel": getattr(bundle, "wifi_channel", None),
        "wifi_security_mode": getattr(bundle, "wifi_security_mode", None),
        "primary_wan_service": primary_wan,
    }


def _empty_values() -> dict[str, Any]:
    """Return empty values when no bundle is assigned."""
    return {
        "config_method": None,
        "onu_mode": None,
        "ip_protocol": None,
        "wan_mode": None,
        "wan_vlan": None,
        "pppoe_username": None,
        "mgmt_ip_mode": None,
        "mgmt_vlan": None,
        "mgmt_ip_address": None,
        "wifi_enabled": None,
        "wifi_ssid": None,
        "wifi_channel": None,
        "wifi_security_mode": None,
        "primary_wan_service": None,
    }


def resolve_effective_ont_config(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> dict[str, Any]:
    """Return effective ONT config as applied bundle + sparse overrides."""
    assignment, bundle, blocked_reason = _resolve_ready_bundle(db, ont, olt=olt)
    raw_overrides = _load_raw_overrides(db, ont)
    overrides = _canonicalize_overrides(raw_overrides)
    has_bundle = assignment is not None or bundle is not None

    if has_bundle:
        values = _bundle_values(
            bundle,
            template_context=_subscriber_template_context(db, ont),
        )
    else:
        values = _empty_values()
    for key, value in overrides.items():
        values[key] = value

    return {
        "bundle": bundle,
        "bundle_assignment": assignment,
        "bundle_assignment_status": getattr(getattr(assignment, "status", None), "value", None),
        "bundle_assignment_blocked_reason": blocked_reason,
        "config_ready": blocked_reason is None,
        "overrides": sorted(overrides.keys()),
        "values": values,
        "using_legacy_fallback": False,
    }
