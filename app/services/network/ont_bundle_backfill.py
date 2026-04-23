"""Backfill legacy ONT desired state into bundle assignment + sparse overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntConfigOverrideSource,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntUnit,
)
from app.services.network.ont_bundle_assignments import assign_bundle_to_ont
from app.services.network.ont_config_overrides import (
    clear_bundle_managed_legacy_projection,
)


@dataclass
class OntBackfillPlan:
    ont_id: str
    serial_number: str
    outcome: str
    reason: str
    bundle_id: str | None = None
    bundle_name: str | None = None
    override_values: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OntBackfillRun:
    plans: list[OntBackfillPlan]
    counts: dict[str, int]


def _enum_or_raw(value: Any) -> Any:
    return getattr(value, "value", value)


def _normalize(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value)


def _legacy_values(ont: OntUnit) -> dict[str, Any]:
    return {
        "config_method": _enum_or_raw(getattr(ont, "config_method", None)),
        "onu_mode": _enum_or_raw(getattr(ont, "onu_mode", None)),
        "ip_protocol": _enum_or_raw(getattr(ont, "ip_protocol", None)),
        "wan.wan_mode": _enum_or_raw(getattr(ont, "wan_mode", None)),
        "wan.vlan_tag": getattr(getattr(ont, "wan_vlan", None), "tag", None),
        "wan.pppoe_username": getattr(ont, "pppoe_username", None),
        "management.ip_mode": _enum_or_raw(getattr(ont, "mgmt_ip_mode", None)),
        "management.vlan_tag": getattr(getattr(ont, "mgmt_vlan", None), "tag", None),
        "management.ip_address": getattr(ont, "mgmt_ip_address", None),
        "wifi.enabled": getattr(ont, "wifi_enabled", None),
        "wifi.ssid": getattr(ont, "wifi_ssid", None),
        "wifi.channel": getattr(ont, "wifi_channel", None),
        "wifi.security_mode": getattr(ont, "wifi_security_mode", None),
    }


def _bundle_values(bundle: OntProvisioningProfile) -> dict[str, Any]:
    services = [
        service
        for service in (getattr(bundle, "wan_services", None) or [])
        if getattr(service, "is_active", False)
    ]
    services.sort(
        key=lambda service: (
            getattr(service, "priority", 9999),
            getattr(service, "name", "") or "",
        )
    )
    primary = services[0] if services else None
    return {
        "config_method": _enum_or_raw(getattr(bundle, "config_method", None)),
        "onu_mode": _enum_or_raw(getattr(bundle, "onu_mode", None)),
        "ip_protocol": _enum_or_raw(getattr(bundle, "ip_protocol", None)),
        "wan.wan_mode": _enum_or_raw(getattr(primary, "connection_type", None)),
        "wan.vlan_tag": getattr(primary, "s_vlan", None),
        "wan.pppoe_username": getattr(primary, "pppoe_username_template", None),
        "management.ip_mode": _enum_or_raw(getattr(bundle, "mgmt_ip_mode", None)),
        "management.vlan_tag": getattr(bundle, "mgmt_vlan_tag", None),
        "management.ip_address": None,
        "wifi.enabled": getattr(bundle, "wifi_enabled", None),
        "wifi.ssid": getattr(bundle, "wifi_ssid_template", None),
        "wifi.channel": getattr(bundle, "wifi_channel", None),
        "wifi.security_mode": getattr(bundle, "wifi_security_mode", None),
    }


def _has_any_legacy_desired_state(ont: OntUnit) -> bool:
    return any(_normalize(value) is not None for value in _legacy_values(ont).values())


def _has_provisioning_history(ont: OntUnit) -> bool:
    return getattr(ont, "provisioning_status", None) in {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.drift_detected,
        OntProvisioningStatus.failed,
    }


def _active_assignment(db: Session, ont: OntUnit) -> OntBundleAssignment | None:
    return db.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .where(OntBundleAssignment.is_active.is_(True))
        .limit(1)
    ).first()


def build_backfill_plan(db: Session, ont: OntUnit) -> OntBackfillPlan:
    active_assignment = _active_assignment(db, ont)
    if active_assignment is not None:
        return OntBackfillPlan(
            ont_id=str(ont.id),
            serial_number=ont.serial_number or "",
            outcome="already_migrated",
            reason="active bundle assignment already exists",
            bundle_id=str(active_assignment.bundle_id),
        )

    existing_override_count = int(
        db.scalar(
            select(func.count())
            .select_from(OntConfigOverride)
            .where(OntConfigOverride.ont_unit_id == ont.id)
        )
        or 0
    )
    if existing_override_count:
        return OntBackfillPlan(
            ont_id=str(ont.id),
            serial_number=ont.serial_number or "",
            outcome="manual_review",
            reason="legacy ONT has overrides without active assignment",
        )

    profile_id = getattr(ont, "provisioning_profile_id", None)
    if not profile_id:
        if _has_any_legacy_desired_state(ont):
            return OntBackfillPlan(
                ont_id=str(ont.id),
                serial_number=ont.serial_number or "",
                outcome="manual_review",
                reason="legacy ONT has desired-state fields but no assigned bundle",
            )
        if _has_provisioning_history(ont):
            return OntBackfillPlan(
                ont_id=str(ont.id),
                serial_number=ont.serial_number or "",
                outcome="manual_review",
                reason=(
                    "ONT has provisioning history but no bundle assignment or "
                    "legacy desired-state config"
                ),
            )
        return OntBackfillPlan(
            ont_id=str(ont.id),
            serial_number=ont.serial_number or "",
            outcome="unconfigured",
            reason="ONT has no bundle and no legacy desired-state config",
        )

    bundle = db.scalars(
        select(OntProvisioningProfile)
        .options(selectinload(OntProvisioningProfile.wan_services))
        .where(OntProvisioningProfile.id == profile_id)
        .limit(1)
    ).first()
    if bundle is None:
        return OntBackfillPlan(
            ont_id=str(ont.id),
            serial_number=ont.serial_number or "",
            outcome="manual_review",
            reason="assigned provisioning profile was not found",
        )
    if not bundle.is_active:
        return OntBackfillPlan(
            ont_id=str(ont.id),
            serial_number=ont.serial_number or "",
            outcome="manual_review",
            reason="assigned provisioning profile is inactive",
            bundle_id=str(bundle.id),
            bundle_name=bundle.name,
        )

    legacy_values = _legacy_values(ont)
    bundle_values = _bundle_values(bundle)
    override_values: dict[str, Any] = {}
    for field_name, legacy_value in legacy_values.items():
        if _normalize(legacy_value) != _normalize(bundle_values.get(field_name)):
            if _normalize(legacy_value) is not None:
                override_values[field_name] = legacy_value

    warnings: list[str] = []
    if getattr(ont, "pppoe_password", None):
        warnings.append("pppoe_password remains in legacy secret storage")
    if getattr(ont, "wifi_password", None):
        warnings.append("wifi_password remains in legacy secret storage")

    return OntBackfillPlan(
        ont_id=str(ont.id),
        serial_number=ont.serial_number or "",
        outcome="backfill",
        reason="assigned profile can be converted into bundle + sparse overrides",
        bundle_id=str(bundle.id),
        bundle_name=bundle.name,
        override_values=override_values,
        warnings=warnings,
    )


def apply_backfill_plan(db: Session, ont: OntUnit, plan: OntBackfillPlan) -> None:
    if plan.outcome != "backfill" or not plan.bundle_id:
        return

    bundle = db.get(OntProvisioningProfile, plan.bundle_id)
    if bundle is None:
        raise ValueError(f"Bundle {plan.bundle_id} not found for ONT {plan.ont_id}")

    assign_bundle_to_ont(
        db,
        ont=ont,
        bundle=bundle,
        status=OntBundleAssignmentStatus.applied,
        assigned_reason="backfill_script",
    )
    clear_bundle_managed_legacy_projection(ont)
    ont.provisioning_profile_id = None

    existing_rows = db.scalars(
        select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
    ).all()
    for row in existing_rows:
        db.delete(row)

    for field_name, value in plan.override_values.items():
        db.add(
            OntConfigOverride(
                ont_unit_id=ont.id,
                field_name=field_name,
                value_json={"value": _normalize(value)},
                source=OntConfigOverrideSource.workflow,
                reason="backfill_script",
            )
        )


def iter_candidate_onts(
    db: Session,
    *,
    ont_id: str | None = None,
    limit: int | None = None,
) -> list[OntUnit]:
    stmt = select(OntUnit).options(
        selectinload(OntUnit.wan_vlan),
        selectinload(OntUnit.mgmt_vlan),
    )
    if ont_id:
        stmt = stmt.where(OntUnit.id == ont_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def run_backfill(
    db: Session,
    *,
    ont_id: str | None = None,
    limit: int | None = None,
    apply: bool = False,
) -> OntBackfillRun:
    onts = iter_candidate_onts(db, ont_id=ont_id, limit=limit)
    plans: list[OntBackfillPlan] = []
    counts = {
        "already_migrated": 0,
        "backfill": 0,
        "manual_review": 0,
        "unconfigured": 0,
    }

    for ont in onts:
        plan = build_backfill_plan(db, ont)
        plans.append(plan)
        counts[plan.outcome] = counts.get(plan.outcome, 0) + 1
        if apply and plan.outcome == "backfill":
            apply_backfill_plan(db, ont, plan)

    return OntBackfillRun(plans=plans, counts=counts)
