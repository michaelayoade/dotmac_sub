"""ONT provisioning profile resolve and apply service.

Bridges the desired state (OntProvisioningProfile) with the observed state
(OntUnit flat fields). Handles profile resolution from subscription/offer,
applying profile config to an ONT, and drift detection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntUnit,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


@dataclass
class DriftField:
    """A single field where desired != observed."""

    field_name: str
    desired: object
    observed: object


@dataclass
class DriftReport:
    """Result of comparing an ONT's current config against its profile."""

    ont_id: str
    profile_id: str
    profile_name: str
    has_drift: bool
    drifted_fields: list[DriftField] = field(default_factory=list)


@dataclass
class ApplyResult:
    """Result of applying a profile to an ONT."""

    success: bool
    message: str
    fields_updated: int = 0


# Fields on OntUnit that map directly from OntProvisioningProfile
_PROFILE_TO_ONT_FIELDS = {
    "config_method": "config_method",
    "onu_mode": "onu_mode",
    "ip_protocol": "ip_protocol",
    "download_speed_profile_id": "download_speed_profile_id",
    "upload_speed_profile_id": "upload_speed_profile_id",
    "mgmt_ip_mode": "mgmt_ip_mode",
    "mgmt_remote_access": "mgmt_remote_access",
    "voip_enabled": "voip_enabled",
}


def _profile_matches_ont_scope(
    profile: OntProvisioningProfile,
    ont: OntUnit,
) -> bool:
    """Return true when a profile is scoped to the ONT's OLT."""
    profile_olt_id = getattr(profile, "olt_device_id", None)
    ont_olt_id = getattr(ont, "olt_device_id", None)
    if not profile_olt_id or not ont_olt_id:
        return profile_olt_id == ont_olt_id
    return profile_olt_id == ont_olt_id


def resolve_profile_for_ont(
    db: Session,
    ont_id: str,
) -> OntProvisioningProfile | None:
    """Resolve the best provisioning profile for an ONT.

    Resolution chain:
    1. Directly assigned profile on OntUnit.provisioning_profile_id
    2. Default profile from the linked CatalogOffer (via subscription → offer)
    3. Business account default profile (is_default=True)
    4. None
    """
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return None

    # 1. Direct assignment
    if ont.provisioning_profile_id:
        stmt = (
            select(OntProvisioningProfile)
            .options(selectinload(OntProvisioningProfile.wan_services))
            .where(OntProvisioningProfile.id == ont.provisioning_profile_id)
        )
        profile = db.scalars(stmt).first()
        if profile and profile.is_active and _profile_matches_ont_scope(profile, ont):
            return profile
        if profile and profile.is_active:
            logger.warning(
                "Ignoring ONT %s profile %s because it is scoped to OLT %s, not %s",
                ont.serial_number,
                profile.name,
                profile.olt_device_id,
                ont.olt_device_id,
            )

    # 2. Business account default (need business subscriber from assignment)
    from app.models.network import OntAssignment

    active_assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).first()
    if active_assignment and active_assignment.subscriber_id:
        from app.models.subscriber import Subscriber

        subscriber = db.get(Subscriber, active_assignment.subscriber_id)
        if subscriber and getattr(subscriber, "is_business", False):
            owner_subscriber_id = getattr(subscriber, "id", None)
            if owner_subscriber_id:
                stmt = (
                    select(OntProvisioningProfile)
                    .options(selectinload(OntProvisioningProfile.wan_services))
                    .where(
                        OntProvisioningProfile.owner_subscriber_id
                        == owner_subscriber_id,
                        OntProvisioningProfile.is_default.is_(True),
                        OntProvisioningProfile.is_active.is_(True),
                        OntProvisioningProfile.olt_device_id == ont.olt_device_id,
                    )
                )
                profile = db.scalars(stmt).first()
                if profile:
                    return profile

    return None


def apply_profile_to_ont(
    db: Session,
    ont_id: str,
    profile_id: str,
) -> ApplyResult:
    """Apply a provisioning profile's desired state to an ONT's flat fields.

    Updates the OntUnit's config fields to match the profile, sets the
    provisioning_profile_id FK, and marks provisioning_status as provisioned.
    """
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return ApplyResult(success=False, message="ONT not found")

    stmt = (
        select(OntProvisioningProfile)
        .options(selectinload(OntProvisioningProfile.wan_services))
        .where(OntProvisioningProfile.id == coerce_uuid(profile_id))
    )
    profile = db.scalars(stmt).first()
    if not profile:
        return ApplyResult(success=False, message="Provisioning profile not found")
    if not _profile_matches_ont_scope(profile, ont):
        return ApplyResult(
            success=False,
            message=(
                f"Profile '{profile.name}' is scoped to another OLT and cannot be "
                "applied to this ONT."
            ),
        )

    fields_updated = 0
    for profile_field, ont_field in _PROFILE_TO_ONT_FIELDS.items():
        desired = getattr(profile, profile_field, None)
        current = getattr(ont, ont_field, None)
        if desired is not None and desired != current:
            setattr(ont, ont_field, desired)
            fields_updated += 1

    # Link profile and update status
    ont.provisioning_profile_id = profile.id
    ont.provisioning_status = OntProvisioningStatus.provisioned
    ont.last_provisioned_at = datetime.now(UTC)

    db.commit()
    db.refresh(ont)

    logger.info(
        "Applied profile %s (%s) to ONT %s, %d fields updated",
        profile.id,
        profile.name,
        ont_id,
        fields_updated,
    )
    return ApplyResult(
        success=True,
        message=f"Profile '{profile.name}' applied successfully. {fields_updated} fields updated.",
        fields_updated=fields_updated,
    )


def detect_drift(db: Session, ont_id: str) -> DriftReport | None:
    """Compare an ONT's current config against its assigned profile.

    Returns a DriftReport listing any fields that differ, or None if
    no profile is assigned.
    """
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont or not ont.provisioning_profile_id:
        return None

    stmt = select(OntProvisioningProfile).where(
        OntProvisioningProfile.id == ont.provisioning_profile_id
    )
    profile = db.scalars(stmt).first()
    if not profile:
        return None

    drifted: list[DriftField] = []
    for profile_field, ont_field in _PROFILE_TO_ONT_FIELDS.items():
        desired = getattr(profile, profile_field, None)
        observed = getattr(ont, ont_field, None)
        if desired is not None and desired != observed:
            drifted.append(
                DriftField(
                    field_name=ont_field,
                    desired=desired,
                    observed=observed,
                )
            )

    has_drift = len(drifted) > 0
    return DriftReport(
        ont_id=str(ont.id),
        profile_id=str(profile.id),
        profile_name=profile.name,
        has_drift=has_drift,
        drifted_fields=drifted,
    )


def detect_drift_batch(
    db: Session,
    *,
    owner_subscriber_id: str | None = None,
    limit: int = 500,
) -> list[DriftReport]:
    """Batch drift detection for all ONTs with assigned profiles.

    Returns list of DriftReports for ONTs where drift is detected.
    """
    stmt = (
        select(OntUnit)
        .where(
            OntUnit.provisioning_profile_id.isnot(None),
            OntUnit.is_active.is_(True),
        )
        .limit(limit)
    )

    # Filter by business account if provided. This is applied after loading
    # because the assignment/subscriber join is not part of the base query.
    onts = list(db.scalars(stmt).all())
    drift_reports: list[DriftReport] = []

    for ont in onts:
        if owner_subscriber_id:
            from app.models.network import OntAssignment

            active_assignment = db.scalars(
                select(OntAssignment).where(
                    OntAssignment.ont_unit_id == ont.id,
                    OntAssignment.active.is_(True),
                )
            ).first()
            if not active_assignment or str(active_assignment.subscriber_id) != str(
                owner_subscriber_id
            ):
                continue
        report = detect_drift(db, str(ont.id))
        if report and report.has_drift:
            # Mark drift on the ONT
            ont.provisioning_status = OntProvisioningStatus.drift_detected
            drift_reports.append(report)

    if drift_reports:
        db.commit()

    return drift_reports
