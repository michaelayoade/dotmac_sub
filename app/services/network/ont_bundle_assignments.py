"""Helpers for explicit ONT bundle assignment state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    OLTDevice,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntProvisioningProfile,
    OntUnit,
)


def is_config_ready_bundle_assignment(
    assignment: OntBundleAssignment | None,
) -> bool:
    """Return True when an active assignment may drive desired config."""
    return (
        assignment is not None
        and assignment.status == OntBundleAssignmentStatus.applied
        and getattr(assignment.bundle, "is_active", False)
    )


def get_active_bundle_assignment(
    db: Session,
    ont: OntUnit | str,
) -> OntBundleAssignment | None:
    """Return the active bundle assignment for an ONT, if present."""
    ont_id = getattr(ont, "id", None) or ont
    stmt = (
        select(OntBundleAssignment)
        .options(selectinload(OntBundleAssignment.bundle))
        .where(OntBundleAssignment.ont_unit_id == ont_id)
        .where(OntBundleAssignment.is_active.is_(True))
        .order_by(OntBundleAssignment.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def resolve_assigned_bundle(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> OntProvisioningProfile | None:
    """Resolve the current bundle for an ONT.

    Resolution order:
    1. Active explicit bundle assignment
    2. OLT default provisioning profile
    """
    assignment = get_active_bundle_assignment(db, ont)
    if assignment is not None:
        if is_config_ready_bundle_assignment(assignment):
            return assignment.bundle
        return None

    olt_obj = olt
    if olt_obj is None:
        olt_id = getattr(ont, "olt_device_id", None)
        if olt_id:
            olt_obj = db.get(OLTDevice, str(olt_id))
    default_profile_id = (
        getattr(olt_obj, "default_provisioning_profile_id", None) if olt_obj else None
    )
    if default_profile_id:
        profile = db.get(OntProvisioningProfile, str(default_profile_id))
        if profile and profile.is_active:
            return profile
    return None


def assign_bundle_to_ont(
    db: Session,
    *,
    ont: OntUnit,
    bundle: OntProvisioningProfile,
    status: OntBundleAssignmentStatus = OntBundleAssignmentStatus.applied,
    assigned_reason: str | None = None,
    assigned_by_subscriber_id: Any | None = None,
) -> OntBundleAssignment:
    """Create or refresh the active bundle assignment for an ONT.
    """
    if not bundle.is_active:
        raise ValueError(f"Cannot assign inactive bundle {bundle.id} to ONT {ont.id}")
    now = datetime.now(UTC)
    active_assignment = get_active_bundle_assignment(db, ont)

    if active_assignment and active_assignment.bundle_id != bundle.id:
        active_assignment.is_active = False
        active_assignment.status = OntBundleAssignmentStatus.superseded
        active_assignment.superseded_at = now

    if active_assignment and active_assignment.bundle_id == bundle.id:
        assignment = active_assignment
    else:
        assignment = OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            assigned_by_subscriber_id=assigned_by_subscriber_id,
            is_active=True,
        )
        db.add(assignment)

    assignment.status = status
    assignment.is_active = True
    assignment.assigned_reason = assigned_reason
    assignment.applied_at = now if status == OntBundleAssignmentStatus.applied else None
    assignment.superseded_at = None
    return assignment


def clear_active_bundle_assignment(
    db: Session,
    *,
    ont: OntUnit,
) -> bool:
    """Deactivate the current bundle assignment."""
    active_assignment = get_active_bundle_assignment(db, ont)
    if active_assignment is None:
        return False

    now = datetime.now(UTC)
    active_assignment.is_active = False
    active_assignment.status = OntBundleAssignmentStatus.superseded
    active_assignment.superseded_at = now
    return True
