"""ONT provisioning profile resolve and apply service.

Bridges the desired state (OntProvisioningProfile) with the observed state
(OntUnit flat fields). Handles profile resolution from subscription/offer,
applying profile config to an ONT, and drift detection.

Phase 2+3 architecture: When applying a profile, creates OntWanServiceInstance
records for each wan_service in the profile. These instances hold resolved
credentials (from templates) and VLAN references, enabling multi-WAN support
and grouped L2/L3 provisioning.
"""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    OntAssignment,
    OntBundleAssignmentStatus,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntUnit,
    OntWanServiceInstance,
    PppoePasswordMode,
    Vlan,
    WanServiceProvisioningStatus,
)
from app.services.common import coerce_uuid
from app.services.network._common import SubscriberTemplateContextProvider
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_bundle_assignments import (
    assign_bundle_to_ont,
    get_active_bundle_assignment,
    resolve_assigned_bundle,
)
from app.services.network.ont_config_overrides import (
    clear_bundle_managed_legacy_projection,
)

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


def _generate_random_password(length: int = 12) -> str:
    """Generate a cryptographically random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _resolve_pppoe_username_template(
    template: str | None,
    *,
    subscriber_code: str = "",
    subscriber_name: str = "",
    serial_number: str = "",
    offer_name: str = "",
    ont_id_short: str = "",
) -> str | None:
    """Resolve a PPPoE username template to an actual username.

    Supports placeholders:
    - {subscriber_code}: Subscriber's external code/ID
    - {subscriber_name}: Subscriber's full name
    - {serial_number}: ONT serial number
    - {offer_name}: Catalog offer name
    - {ont_id_short}: Short ONT identifier (first 8 chars of UUID)
    """
    if not template:
        return None
    result = template
    result = result.replace("{subscriber_code}", subscriber_code)
    result = result.replace("{subscriber_name}", subscriber_name)
    result = result.replace("{serial_number}", serial_number)
    result = result.replace("{offer_name}", offer_name)
    result = result.replace("{ont_id_short}", ont_id_short)
    return result


def _resolve_vlan_by_tag(
    db: Session,
    vlan_tag: int | None,
    olt_device_id: object | None,
) -> Vlan | None:
    """Look up a VLAN record by tag, scoped to OLT if provided."""
    if not vlan_tag:
        return None
    stmt = select(Vlan).where(
        Vlan.tag == vlan_tag,
        Vlan.is_active.is_(True),
    )
    if olt_device_id:
        stmt = stmt.where(Vlan.olt_device_id == olt_device_id)
    else:
        return None
    return db.scalars(stmt).first()


def _get_subscriber_context(
    db: Session,
    ont: OntUnit,
    subscriber_context_provider: SubscriberTemplateContextProvider | None = None,
) -> dict[str, str]:
    """Build subscriber context for template resolution."""
    context = {
        "subscriber_code": "",
        "subscriber_name": "",
        "serial_number": ont.serial_number or "",
        "offer_name": "",
        "ont_id_short": str(ont.id)[:8] if ont.id else "",
    }

    # Get subscriber from active assignment
    active_assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).first()
    if (
        active_assignment
        and active_assignment.subscriber_id
        and subscriber_context_provider is not None
    ):
        context.update(
            subscriber_context_provider.get_template_context(
                db,
                subscriber_id=active_assignment.subscriber_id,
            )
        )

    return context


def _create_wan_service_instances(
    db: Session,
    ont: OntUnit,
    profile: OntProvisioningProfile,
    subscriber_context: dict[str, str],
) -> int:
    """Create OntWanServiceInstance records from profile's wan_services.

    Returns the number of service instances created.
    """
    from app.services.credential_crypto import decrypt_credential, encrypt_credential

    # Remove existing service instances for this ONT (replace strategy)
    existing_instances = db.scalars(
        select(OntWanServiceInstance).where(
            OntWanServiceInstance.ont_id == ont.id,
        )
    ).all()
    for instance in existing_instances:
        db.delete(instance)

    # Get profile's WAN services
    wan_services = profile.wan_services or []
    active_services = [s for s in wan_services if s.is_active]

    if not active_services:
        logger.debug("Profile %s has no active WAN services", profile.id)
        return 0

    created = 0
    effective = resolve_effective_ont_config(db, ont)
    effective_values = effective.get("values", {}) if isinstance(effective, dict) else {}
    for profile_service in active_services:
        # Resolve PPPoE username from template
        pppoe_username = _resolve_pppoe_username_template(
            profile_service.pppoe_username_template,
            **subscriber_context,
        )
        if (
            profile_service.connection_type.value == "pppoe"
            and not pppoe_username
            and effective_values.get("pppoe_username")
        ):
            pppoe_username = str(effective_values.get("pppoe_username"))

        # Resolve PPPoE password based on mode
        pppoe_password: str | None = None
        password_mode = profile_service.pppoe_password_mode
        if password_mode == PppoePasswordMode.static:
            # Use static password from profile (decrypt then re-encrypt)
            if profile_service.pppoe_static_password:
                try:
                    plain = decrypt_credential(profile_service.pppoe_static_password)
                    pppoe_password = encrypt_credential(plain) if plain else None
                except Exception:
                    pppoe_password = None
        elif password_mode == PppoePasswordMode.generate:
            # Generate random password
            plain = _generate_random_password(12)
            pppoe_password = encrypt_credential(plain)
        elif password_mode == PppoePasswordMode.from_credential:
            # Credential-sourced passwords will be resolved at provisioning time
            # (from AccessCredential table)
            pppoe_password = None
        if (
            profile_service.connection_type.value == "pppoe"
            and pppoe_password is None
            and getattr(ont, "pppoe_password", None)
        ):
            pppoe_password = ont.pppoe_password

        # Resolve VLAN by tag
        vlan = _resolve_vlan_by_tag(
            db,
            profile_service.s_vlan,
            ont.olt_device_id,
        )

        instance = OntWanServiceInstance(
            ont_id=ont.id,
            source_profile_service_id=profile_service.id,
            service_type=profile_service.service_type,
            name=profile_service.name,
            priority=profile_service.priority,
            is_active=True,
            # L2 VLAN
            vlan_mode=profile_service.vlan_mode,
            vlan_id=vlan.id if vlan else None,
            s_vlan=profile_service.s_vlan,
            c_vlan=profile_service.c_vlan,
            # L3 connection
            connection_type=profile_service.connection_type,
            nat_enabled=profile_service.nat_enabled,
            # PPPoE
            pppoe_username=pppoe_username,
            pppoe_password=pppoe_password,
            # Provisioning state
            provisioning_status=WanServiceProvisioningStatus.pending,
        )
        db.add(instance)
        created += 1
        logger.debug(
            "Created WAN service instance %s (%s) for ONT %s",
            profile_service.service_type.value,
            profile_service.name or "unnamed",
            ont.serial_number,
        )

    return created


def _profile_scope_mismatch_reason(
    db: Session,
    profile: OntProvisioningProfile,
    ont: OntUnit,
) -> str | None:
    """Return why a profile cannot apply to an ONT, or None when allowed."""
    profile_olt_id = getattr(profile, "olt_device_id", None)
    ont_olt_id = getattr(ont, "olt_device_id", None)
    if profile_olt_id != ont_olt_id:
        return "olt_scope"

    owner_subscriber_id = getattr(profile, "owner_subscriber_id", None)
    if not owner_subscriber_id:
        return None

    assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    ).first()
    if not assignment or assignment.subscriber_id != owner_subscriber_id:
        return "owner_subscriber_scope"
    return None


def resolve_profile_for_ont(
    db: Session,
    ont_id: str,
) -> OntProvisioningProfile | None:
    """Resolve the best provisioning profile for an ONT.

    Resolution chain:
    1. Active explicit bundle assignment
    2. None
    """
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return None

    profile = resolve_assigned_bundle(db, ont)
    if (
        profile
        and profile.is_active
        and _profile_scope_mismatch_reason(db, profile, ont) is None
    ):
        return profile
    if profile and profile.is_active:
        reason = _profile_scope_mismatch_reason(db, profile, ont)
        logger.warning(
            "Ignoring ONT %s profile %s because scope does not match "
            "(reason=%s profile_olt=%s ont_olt=%s profile_owner=%s)",
            ont.serial_number,
            profile.name,
            reason,
            profile.olt_device_id,
            ont.olt_device_id,
            profile.owner_subscriber_id,
        )

    return None


def apply_bundle_to_ont(
    db: Session,
    ont_id: str,
    bundle_id: str,
    *,
    create_wan_instances: bool = True,
    push_to_device: bool = False,
    subscriber_context_provider: SubscriberTemplateContextProvider | None = None,
) -> ApplyResult:
    """Apply a provisioning bundle's desired state to an ONT.

    Updates the OntUnit's config fields to match the profile, sets the
    provisioning_profile_id FK, and marks provisioning_status as provisioned.

    When create_wan_instances=True (default), also creates OntWanServiceInstance
    records for each WAN service in the profile. These instances hold resolved
    PPPoE credentials and VLAN references for multi-WAN provisioning.

    When push_to_device=True, after applying to the DB, also pushes the
    configuration to the actual device via OLT SSH commands. This ensures
    the device matches the desired state.

    Args:
        db: Database session.
        ont_id: UUID of the ONT to apply the bundle to.
        bundle_id: UUID of the provisioning bundle.
        create_wan_instances: If True, create WAN service instances from bundle.
        push_to_device: If True, push changes to the device after DB update.

    Returns:
        ApplyResult with success status and message.
    """
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return ApplyResult(success=False, message="ONT not found")

    stmt = (
        select(OntProvisioningProfile)
        .options(selectinload(OntProvisioningProfile.wan_services))
        .where(OntProvisioningProfile.id == coerce_uuid(bundle_id))
    )
    profile = db.scalars(stmt).first()
    if not profile:
        return ApplyResult(success=False, message="Provisioning bundle not found")
    if not profile.is_active:
        return ApplyResult(
            success=False,
            message=f"Provisioning bundle '{profile.name}' is inactive",
        )
    mismatch_reason = _profile_scope_mismatch_reason(db, profile, ont)
    if mismatch_reason == "olt_scope":
        return ApplyResult(
            success=False,
            message=(
                f"Profile '{profile.name}' is scoped to another OLT and cannot be "
                "applied to this ONT."
            ),
        )
    if mismatch_reason == "owner_subscriber_scope":
        return ApplyResult(
            success=False,
            message=(
                f"Profile '{profile.name}' is scoped to another business account "
                "and cannot be applied to this ONT."
            ),
        )

    fields_updated = 0

    assign_bundle_to_ont(
        db,
        ont=ont,
        bundle=profile,
        status=OntBundleAssignmentStatus.applied,
        assigned_reason="bundle_apply_service",
    )
    clear_bundle_managed_legacy_projection(ont)
    ont.provisioning_status = OntProvisioningStatus.provisioned
    ont.last_provisioned_at = datetime.now(UTC)

    # Create WAN service instances from profile's wan_services
    wan_instances_created = 0
    if create_wan_instances and profile.wan_services:
        subscriber_context = _get_subscriber_context(
            db,
            ont,
            subscriber_context_provider=subscriber_context_provider,
        )
        wan_instances_created = _create_wan_service_instances(
            db, ont, profile, subscriber_context
        )

    db.flush()

    logger.info(
        "Applied bundle %s (%s) to ONT %s: %d legacy fields projected, %d WAN instances created",
        profile.id,
        profile.name,
        ont_id,
        fields_updated,
        wan_instances_created,
    )
    message = (
        f"Bundle '{profile.name}' applied successfully. "
        f"{fields_updated} legacy fields projected"
    )
    if wan_instances_created:
        message += f", {wan_instances_created} WAN service instances created"
    message += "."

    # Push to device if requested
    if push_to_device:
        from app.services.network.ont_profile_push import OntProfilePushService

        push_result = OntProfilePushService.push_profile_to_device(db, ont_id)
        if not push_result.success:
            return ApplyResult(
                success=False,
                message=f"Bundle applied to DB but device push failed: {push_result.message}",
                fields_updated=fields_updated,
            )
        message += f" Device push: {len(push_result.fields_pushed)} field(s) pushed."

    return ApplyResult(
        success=True,
        message=message,
        fields_updated=fields_updated,
    )

def detect_drift(db: Session, ont_id: str) -> DriftReport | None:
    """Compare an ONT's current config against its assigned profile.

    Returns a DriftReport listing any fields that differ, or None if
    no profile is assigned.
    """
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return None
    profile = resolve_assigned_bundle(db, ont)
    if not profile:
        return None
    if _profile_scope_mismatch_reason(db, profile, ont) is not None:
        return None

    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}

    drifted: list[DriftField] = []
    for profile_field, ont_field in _PROFILE_TO_ONT_FIELDS.items():
        desired = getattr(profile, profile_field, None)
        if ont_field in {"config_method", "onu_mode", "ip_protocol", "mgmt_ip_mode"}:
            observed = values.get(ont_field)
        else:
            observed = getattr(ont, ont_field, None)
        desired_cmp = getattr(desired, "value", desired)
        observed_cmp = getattr(observed, "value", observed)
        if desired is not None and desired_cmp != observed_cmp:
            drifted.append(
                DriftField(
                    field_name=ont_field,
                    desired=desired_cmp,
                    observed=observed_cmp,
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
    stmt = select(OntUnit).where(OntUnit.is_active.is_(True)).limit(limit)

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
        if get_active_bundle_assignment(db, ont) is None:
            continue
        report = detect_drift(db, str(ont.id))
        if report and report.has_drift:
            # Mark drift on the ONT
            ont.provisioning_status = OntProvisioningStatus.drift_detected
            drift_reports.append(report)

    if drift_reports:
        db.commit()

    return drift_reports
