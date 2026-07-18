from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.field_vendor import FieldVendor
from app.models.organization import Organization, OrganizationAccountType
from app.models.party import (
    PartnerRoleKey,
    PartyRole,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.models.subscriber import Reseller
from app.models.vendor_routes import Vendor
from app.services import party as party_service


def _profiles(db_session):
    organization = Organization(
        name="ABC Networks Ltd",
        account_type=OrganizationAccountType.reseller.value,
    )
    reseller = Reseller(name="ABC Networks Channel", code="ABC-CHANNEL")
    vendor = Vendor(name="ABC Networks Supply", code="ABC-SUPPLY")
    db_session.add_all((organization, reseller, vendor))
    db_session.flush()
    field_vendor = FieldVendor(
        crm_vendor_id=str(vendor.id),
        name=vendor.name,
        code=vendor.code,
    )
    db_session.add(field_vendor)
    db_session.flush()
    return organization, reseller, vendor, field_vendor


def _bind_all(db_session, identity, organization, reseller, vendor):
    evidence = {
        "source": "reviewed_organization_profile_worklist",
        "reason": "Reviewed as one legal organization with distinct profiles",
    }
    party_service.bind_organization_profile(
        db_session,
        organization_id=organization.id,
        party_id=identity.id,
        **evidence,
    )
    party_service.bind_reseller_profile(
        db_session,
        reseller_id=reseller.id,
        party_id=identity.id,
        **evidence,
    )
    party_service.bind_vendor_profiles(
        db_session,
        vendor_id=vendor.id,
        party_id=identity.id,
        **evidence,
    )


def test_one_organization_party_can_own_all_profiles_and_independent_roles(db_session):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="ABC Networks Ltd",
    )
    organization, reseller, vendor, field_vendor = _profiles(db_session)

    _bind_all(db_session, identity, organization, reseller, vendor)

    assert organization.party_id == identity.id
    assert reseller.party_id == identity.id
    assert vendor.party_id == identity.id
    assert field_vendor.party_id == identity.id
    assert vendor.party_bound_at == field_vendor.party_bound_at
    assert vendor.party_binding_source == field_vendor.party_binding_source
    assert vendor.party_binding_reason == field_vendor.party_binding_reason
    assert identity.organization_profile is organization
    assert identity.reseller_profile is reseller
    assert identity.vendor_profile is vendor
    assert identity.field_vendor_profile is field_vendor
    assert db_session.query(PartyRole).count() == 0

    reseller_role = party_service.assign_role(
        db_session,
        party_id=identity.id,
        role_type=PartyRoleType.reseller,
        status=PartyRoleStatus.active,
    )
    vendor_role = party_service.assign_role(
        db_session,
        party_id=identity.id,
        role_type=PartyRoleType.vendor,
        status=PartyRoleStatus.active,
    )
    infrastructure_partner = party_service.assign_role(
        db_session,
        party_id=identity.id,
        role_type=PartyRoleType.partner,
        role_key=PartnerRoleKey.infrastructure.value,
        status=PartyRoleStatus.active,
    )
    party_service.transition_role(
        db_session,
        role_id=vendor_role.id,
        status=PartyRoleStatus.suspended,
    )

    assert reseller_role.status == PartyRoleStatus.active.value
    assert vendor_role.status == PartyRoleStatus.suspended.value
    assert infrastructure_partner.role_key == "infrastructure"
    assert organization.account_type == OrganizationAccountType.reseller.value


def test_profile_binding_requires_organization_party_and_assigns_no_role(db_session):
    person = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Jane Doe",
    )
    organization = Organization(name="Personal Company")
    reseller = Reseller(name="Personal Reseller")
    db_session.add_all((organization, reseller))
    db_session.flush()

    with pytest.raises(
        party_service.PartyInvariantError, match="must be an organization"
    ):
        party_service.bind_organization_profile(
            db_session,
            organization_id=organization.id,
            party_id=person.id,
            source="reviewed_worklist",
            reason="Invalid person target",
        )
    with pytest.raises(
        party_service.PartyInvariantError, match="must be an organization"
    ):
        party_service.bind_reseller_profile(
            db_session,
            reseller_id=reseller.id,
            party_id=person.id,
            source="reviewed_worklist",
            reason="Invalid person target",
        )
    assert organization.party_id is None
    assert reseller.party_id is None
    assert db_session.query(PartyRole).count() == 0


def test_exact_profile_retry_preserves_evidence_and_rebind_is_refused(db_session):
    first_party = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="First Organization",
    )
    second_party = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Second Organization",
    )
    organization = Organization(name="Bound Organization")
    db_session.add(organization)
    db_session.flush()
    first = party_service.bind_organization_profile(
        db_session,
        organization_id=organization.id,
        party_id=first_party.id,
        source="initial_review",
        reason="Initial approved binding",
    )
    original_bound_at = first.party_bound_at

    retry = party_service.bind_organization_profile(
        db_session,
        organization_id=organization.id,
        party_id=first_party.id,
        source="retry",
        reason="Retry must not replace provenance",
    )

    assert retry.party_bound_at == original_bound_at
    assert retry.party_binding_source == "initial_review"
    assert retry.party_binding_reason == "Initial approved binding"
    with pytest.raises(
        party_service.PartyInvariantError,
        match="reviewed merge/repoint workflow",
    ):
        party_service.bind_organization_profile(
            db_session,
            organization_id=organization.id,
            party_id=second_party.id,
            source="manual_override",
            reason="Unreviewed target change",
        )


def test_one_party_cannot_own_two_profiles_of_the_same_type(db_session):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Single Reseller Identity",
    )
    first = Reseller(name="First Reseller")
    second = Reseller(name="Second Reseller")
    db_session.add_all((first, second))
    db_session.flush()
    party_service.bind_reseller_profile(
        db_session,
        reseller_id=first.id,
        party_id=identity.id,
        source="reviewed_worklist",
        reason="First profile",
    )

    with pytest.raises(party_service.PartyInvariantError, match="another Reseller"):
        party_service.bind_reseller_profile(
            db_session,
            reseller_id=second.id,
            party_id=identity.id,
            source="reviewed_worklist",
            reason="Duplicate profile",
        )


def test_vendor_and_field_vendor_bind_atomically_and_require_exact_twin(db_session):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Vendor Identity",
    )
    vendor = Vendor(name="Vendor Without Twin")
    db_session.add(vendor)
    db_session.flush()
    with pytest.raises(party_service.PartyInvariantError, match="no exact FieldVendor"):
        party_service.bind_vendor_profiles(
            db_session,
            vendor_id=vendor.id,
            party_id=identity.id,
            source="reviewed_worklist",
            reason="Twin is required",
        )
    assert vendor.party_id is None

    twin = FieldVendor(crm_vendor_id=str(vendor.id), name=vendor.name)
    blocking_projection = FieldVendor(
        crm_vendor_id="legacy-unrelated-vendor",
        name="Existing Projection",
        party_id=identity.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="prior_review",
        party_binding_reason="Existing distinct projection",
    )
    db_session.add_all((twin, blocking_projection))
    db_session.flush()

    with pytest.raises(party_service.PartyInvariantError, match="another FieldVendor"):
        party_service.bind_vendor_profiles(
            db_session,
            vendor_id=vendor.id,
            party_id=identity.id,
            source="reviewed_worklist",
            reason="Atomic collision test",
        )
    assert vendor.party_id is None
    assert twin.party_id is None


def test_vendor_pair_refuses_partial_or_conflicting_existing_binding(db_session):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Vendor Identity",
    )
    other = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Other Vendor Identity",
    )
    vendor = Vendor(
        name="Partially Bound Vendor",
        party_id=identity.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="legacy_review",
        party_binding_reason="Only native profile was bound",
    )
    db_session.add(vendor)
    db_session.flush()
    twin = FieldVendor(
        crm_vendor_id=str(vendor.id),
        name=vendor.name,
    )
    db_session.add(twin)
    db_session.flush()

    with pytest.raises(
        party_service.PartyInvariantError, match="partial or conflicting"
    ):
        party_service.bind_vendor_profiles(
            db_session,
            vendor_id=vendor.id,
            party_id=identity.id,
            source="reviewed_worklist",
            reason="Partial state cannot be silently repaired",
        )
    twin.party_id = other.id
    twin.party_bound_at = datetime.now(UTC)
    twin.party_binding_source = "other_review"
    twin.party_binding_reason = "Conflicting binding"
    db_session.flush()
    with pytest.raises(
        party_service.PartyInvariantError, match="partial or conflicting"
    ):
        party_service.bind_vendor_profiles(
            db_session,
            vendor_id=vendor.id,
            party_id=identity.id,
            source="reviewed_worklist",
            reason="Conflict cannot be silently repointed",
        )


def test_database_requires_complete_profile_binding_provenance(db_session):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Constraint Identity",
    )
    organization = Organization(name="Invalid Direct Binding")
    db_session.add(organization)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            organization.party_id = identity.id
            organization.party_bound_at = datetime.now(UTC)
            db_session.flush()
