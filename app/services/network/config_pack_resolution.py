"""Explicit config-pack resolution stage for ONT provisioning."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_provisioning.result import StepResult

_STEP_NAME = "resolve_effective_config_pack"
_ALLOWED_WAN_PROVISIONING_MODES = {
    "tr069_only",
    "home_gateway_config",
    "omci_wan_config",
}


def _sanitize_raw_config_pack(pack: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(pack)
    sanitized.pop("cr_username", None)
    sanitized.pop("cr_password", None)
    return sanitized


def _effective_value_snapshot(values: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "wan_mode",
        "wan_vlan",
        "wan_gem_index",
        "mgmt_vlan",
        "mgmt_gem_index",
        "tr069_acs_server_id",
        "tr069_olt_profile_id",
        "internet_config_ip_index",
        "wan_config_profile_id",
        "wan_provisioning_mode",
        "pppoe_wcd_index",
        "mgmt_wcd_index",
        "voip_wcd_index",
        "authorization_line_profile_id",
        "authorization_service_profile_id",
        "profile_bundle_id",
        "primary_wan_service",
    )
    return {key: values.get(key) for key in keys}


def _normalize_wan_mode(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"bridged", "setup_via_onu"}:
        return "bridge"
    return normalized


def _build_snapshot(
    *,
    olt: OLTDevice | None,
    raw_config_pack: dict[str, Any],
    resolved_config_pack: dict[str, Any] | None,
    values: dict[str, Any],
    desired_config_keys: list[str],
    validation: dict[str, Any],
    failure_class: str | None = None,
) -> dict[str, Any]:
    snapshot = {
        "olt_id": str(getattr(olt, "id", "") or ""),
        "olt_name": getattr(olt, "name", None),
        "mgmt_ip_pool_id": (
            str(getattr(olt, "mgmt_ip_pool_id", "") or "")
            if getattr(olt, "mgmt_ip_pool_id", None)
            else None
        ),
        "raw_config_pack": _sanitize_raw_config_pack(raw_config_pack),
        "resolved_config_pack": resolved_config_pack,
        "effective_values": _effective_value_snapshot(values),
        "desired_config_keys": desired_config_keys,
        "validation": validation,
    }
    if failure_class is not None:
        snapshot["failure_class"] = failure_class
    return snapshot


def resolve_effective_config_pack_stage(
    db: Session,
    ont: OntUnit,
    *,
    effective_config: dict[str, Any] | None = None,
    olt: OLTDevice | None = None,
) -> tuple[dict[str, Any] | None, StepResult]:
    """Resolve and validate the effective config pack before provisioning writes."""
    t0 = time.monotonic()
    resolved_olt = olt
    if resolved_olt is None and getattr(ont, "olt_device_id", None):
        resolved_olt = db.get(OLTDevice, ont.olt_device_id)

    if resolved_olt is None:
        duration_ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            _STEP_NAME,
            False,
            "Cannot resolve the assigned OLT for config-pack resolution.",
            duration_ms=duration_ms,
            data=_build_snapshot(
                olt=None,
                raw_config_pack={},
                resolved_config_pack=None,
                values={},
                desired_config_keys=[],
                validation={
                    "mismatches": [],
                    "incomplete_fields": ["assigned_olt"],
                    "wan_profile_behavior": "unknown",
                    "effective_tr069_vlan_tag": None,
                    "tr069_vlan_source": None,
                },
                failure_class="regional_pack_resolution_failure",
            ),
        )
        return None, result

    try:
        resolved = (
            effective_config
            if effective_config is not None
            else resolve_effective_ont_config(db, ont, olt=resolved_olt)
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            _STEP_NAME,
            False,
            f"Config-pack resolution failed for OLT {resolved_olt.name}: {exc}",
            duration_ms=duration_ms,
            data=_build_snapshot(
                olt=resolved_olt,
                raw_config_pack=dict(getattr(resolved_olt, "config_pack", None) or {}),
                resolved_config_pack=None,
                values={},
                desired_config_keys=[],
                validation={
                    "mismatches": [],
                    "incomplete_fields": [],
                    "wan_profile_behavior": "unknown",
                    "effective_tr069_vlan_tag": None,
                    "tr069_vlan_source": None,
                },
                failure_class="regional_pack_resolution_failure",
            ),
        )
        return None, result

    raw_config_pack = dict(getattr(resolved_olt, "config_pack", None) or {})
    config_pack = resolved.get("config_pack") if isinstance(resolved, dict) else None
    values = resolved.get("values", {}) if isinstance(resolved, dict) else {}
    desired_config_keys = (
        list(resolved.get("desired_config_keys", []))
        if isinstance(resolved, dict)
        else []
    )
    if not isinstance(values, dict):
        values = {}

    if not raw_config_pack or config_pack is None:
        duration_ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            _STEP_NAME,
            False,
            f"OLT {resolved_olt.name} is missing the runtime config pack required for provisioning.",
            duration_ms=duration_ms,
            data=_build_snapshot(
                olt=resolved_olt,
                raw_config_pack=raw_config_pack,
                resolved_config_pack=config_pack.to_dict() if config_pack else None,
                values=values,
                desired_config_keys=desired_config_keys,
                validation={
                    "mismatches": [],
                    "incomplete_fields": ["config_pack"],
                    "wan_profile_behavior": "unknown",
                    "effective_tr069_vlan_tag": None,
                    "tr069_vlan_source": None,
                },
                failure_class="config_pack_missing",
            ),
        )
        return None, result

    mismatches: list[str] = []
    if (
        raw_config_pack.get("internet_vlan_id")
        and config_pack.internet_vlan.tag is None
    ):
        mismatches.append("internet_vlan_id does not resolve to a live OLT VLAN")
    if (
        raw_config_pack.get("management_vlan_id")
        and config_pack.management_vlan.tag is None
    ):
        mismatches.append("management_vlan_id does not resolve to a live OLT VLAN")
    if raw_config_pack.get("tr069_vlan_id") and config_pack.tr069_vlan.tag is None:
        mismatches.append("tr069_vlan_id does not resolve to a live OLT VLAN")
    if (
        raw_config_pack.get("tr069_olt_profile_id")
        and config_pack.tr069_olt_profile_id is None
    ):
        mismatches.append(
            "tr069_olt_profile_id is present but does not resolve to an integer"
        )

    wan_provisioning_mode = str(
        values.get("wan_provisioning_mode") or config_pack.wan_provisioning_mode or ""
    ).strip()
    if wan_provisioning_mode not in _ALLOWED_WAN_PROVISIONING_MODES:
        mismatches.append(
            f"wan_provisioning_mode '{wan_provisioning_mode}' is not supported"
        )

    effective_tr069_vlan_tag = (
        config_pack.tr069_vlan.tag
        if config_pack.tr069_vlan.tag is not None
        else config_pack.management_vlan.tag
    )
    tr069_vlan_source = (
        "tr069_vlan"
        if config_pack.tr069_vlan.tag is not None
        else (
            "management_vlan" if config_pack.management_vlan.tag is not None else None
        )
    )

    current_wan_mode = _normalize_wan_mode(values.get("wan_mode"))
    current_wan_routed = current_wan_mode not in {None, "bridge"}
    if config_pack.wan_config_profile_id is None:
        if wan_provisioning_mode == "omci_wan_config" and getattr(
            resolved_olt, "supports_ont_wan_config", False
        ):
            wan_profile_behavior = "wan_config_optional_or_missing"
        elif not getattr(resolved_olt, "supports_ont_wan_config", False):
            wan_profile_behavior = "wan_config_skipped_by_capability"
        else:
            wan_profile_behavior = "wan_config_not_required"
    else:
        wan_profile_behavior = "wan_config_profile_resolved"

    incomplete_fields: list[str] = []
    if config_pack.internet_vlan.tag is None:
        incomplete_fields.append("internet_vlan")
    if config_pack.management_vlan.tag is None:
        incomplete_fields.append("management_vlan")
    if effective_tr069_vlan_tag is None:
        incomplete_fields.append("tr069_vlan")
    if config_pack.tr069_acs_server_id is None:
        incomplete_fields.append("tr069_acs_server_id")
    if config_pack.tr069_olt_profile_id is None:
        incomplete_fields.append("tr069_olt_profile_id")
    if getattr(resolved_olt, "mgmt_ip_pool_id", None) is None:
        incomplete_fields.append("mgmt_ip_pool_id")
    if wan_provisioning_mode == "omci_wan_config" and current_wan_routed:
        if values.get("mgmt_wcd_index") is None:
            incomplete_fields.append("mgmt_wcd_index")
        if values.get("internet_config_ip_index") is None:
            incomplete_fields.append("internet_config_ip_index")
        if current_wan_mode == "pppoe" and values.get("pppoe_wcd_index") is None:
            incomplete_fields.append("pppoe_wcd_index")

    for prefix in ("mgmt", "internet"):
        inbound = getattr(config_pack, f"{prefix}_traffic_table_inbound")
        outbound = getattr(config_pack, f"{prefix}_traffic_table_outbound")
        if (inbound is None) ^ (outbound is None):
            incomplete_fields.append(f"{prefix}_traffic_tables")

    validation = {
        "mismatches": mismatches,
        "incomplete_fields": incomplete_fields,
        "wan_profile_behavior": wan_profile_behavior,
        "effective_tr069_vlan_tag": effective_tr069_vlan_tag,
        "tr069_vlan_source": tr069_vlan_source,
        "current_wan_mode": current_wan_mode,
        "wan_provisioning_mode": wan_provisioning_mode,
    }
    snapshot = _build_snapshot(
        olt=resolved_olt,
        raw_config_pack=raw_config_pack,
        resolved_config_pack=config_pack.to_dict(),
        values=values,
        desired_config_keys=desired_config_keys,
        validation=validation,
    )

    duration_ms = int((time.monotonic() - t0) * 1000)
    if mismatches:
        snapshot["failure_class"] = "config_pack_mismatch"
        result = StepResult(
            _STEP_NAME,
            False,
            f"Config-pack mismatch for OLT {resolved_olt.name}: {mismatches[0]}",
            duration_ms=duration_ms,
            data=snapshot,
        )
        return None, result

    if incomplete_fields:
        snapshot["failure_class"] = "config_pack_incomplete"
        result = StepResult(
            _STEP_NAME,
            False,
            f"Config-pack incomplete for OLT {resolved_olt.name}: {incomplete_fields[0]} is required before provisioning.",
            duration_ms=duration_ms,
            data=snapshot,
        )
        return None, result

    result = StepResult(
        _STEP_NAME,
        True,
        f"Resolved effective config pack for OLT {resolved_olt.name}.",
        duration_ms=duration_ms,
        data=snapshot,
    )
    return resolved, result
