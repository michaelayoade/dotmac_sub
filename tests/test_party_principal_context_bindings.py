from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.organization import (
    Organization,
    OrganizationMembership,
    OrganizationMembershipRole,
)
from app.models.party import (
    PartyMembership,
    PartyMembershipStatus,
    PartyMembershipType,
    PartyRole,
    PartyType,
)
from app.models.rbac import SystemUserPermission, SystemUserRole
from app.models.subscriber import Reseller, ResellerUser, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services import party as party_service

_EVIDENCE = {
    "source": "reviewed_principal_context_worklist",
    "reason": "Reviewed Person and organization context",
}


def _system_user(db_session, *, email: str = "staff@example.test") -> SystemUser:
    row = SystemUser(
        first_name="Private",
        last_name="Staff",
        email=email,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _person(db_session, name: str = "Private Person"):
    return party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name=name,
    )


def _organization_party(db_session, name: str = "Private Organization"):
    return party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name=name,
    )


def _membership(db_session, *, person, organization, membership_type):
    return party_service.add_membership(
        db_session,
        person_party_id=person.id,
        organization_party_id=organization.id,
        membership_type=membership_type,
        status=PartyMembershipStatus.invited,
        source="reviewed_context",
    )


def test_system_user_binding_is_identity_only_and_exact_retry_preserves_evidence(
    db_session,
):
    person = _person(db_session)
    system_user = _system_user(db_session)

    first = party_service.bind_system_user_principal(
        db_session,
        system_user_id=system_user.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    original = (
        first.party_bound_at,
        first.party_binding_source,
        first.party_binding_reason,
    )
    retried = party_service.bind_system_user_principal(
        db_session,
        system_user_id=system_user.id,
        person_party_id=person.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is first
    assert retried.person_party_id == person.id
    assert retried.person_party is person
    assert (
        retried.party_bound_at,
        retried.party_binding_source,
        retried.party_binding_reason,
    ) == original
    assert retried.is_active is True
    assert db_session.query(PartyRole).count() == 0
    assert db_session.query(SystemUserRole).count() == 0
    assert db_session.query(SystemUserPermission).count() == 0


def test_system_user_binding_rejects_non_person_duplicate_and_repoint(db_session):
    first_person = _person(db_session, "First")
    second_person = _person(db_session, "Second")
    organization = _organization_party(db_session)
    first_user = _system_user(db_session, email="first@example.test")
    second_user = _system_user(db_session, email="second@example.test")
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=first_user.id,
        person_party_id=first_person.id,
        **_EVIDENCE,
    )

    with pytest.raises(party_service.PartyInvariantError, match="must be a person"):
        party_service.bind_system_user_principal(
            db_session,
            system_user_id=second_user.id,
            person_party_id=organization.id,
            **_EVIDENCE,
        )
    with pytest.raises(party_service.PartyInvariantError, match="already bound"):
        party_service.bind_system_user_principal(
            db_session,
            system_user_id=second_user.id,
            person_party_id=first_person.id,
            **_EVIDENCE,
        )
    with pytest.raises(party_service.PartyInvariantError, match="merge/repoint"):
        party_service.bind_system_user_principal(
            db_session,
            system_user_id=first_user.id,
            person_party_id=second_person.id,
            **_EVIDENCE,
        )


def test_reseller_principal_requires_matching_explicit_membership(db_session):
    person = _person(db_session)
    organization_party = _organization_party(db_session)
    other_organization_party = _organization_party(db_session, "Other Org")
    reseller = Reseller(name="Private Reseller", is_active=True)
    reseller_user = ResellerUser(
        reseller_id=None,
        email="reseller@example.test",
        full_name="Private Reseller User",
        is_active=True,
    )
    db_session.add_all((reseller, reseller_user))
    db_session.flush()
    reseller_user.reseller_id = reseller.id
    party_service.bind_reseller_profile(
        db_session,
        reseller_id=reseller.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    wrong_context = _membership(
        db_session,
        person=person,
        organization=other_organization_party,
        membership_type=PartyMembershipType.reseller_admin,
    )

    with pytest.raises(
        party_service.PartyInvariantError, match="does not match Reseller"
    ):
        party_service.bind_reseller_user_principal(
            db_session,
            reseller_user_id=reseller_user.id,
            person_party_id=person.id,
            party_membership_id=wrong_context.id,
            **_EVIDENCE,
        )

    membership = _membership(
        db_session,
        person=person,
        organization=organization_party,
        membership_type=PartyMembershipType.reseller_admin,
    )
    bound = party_service.bind_reseller_user_principal(
        db_session,
        reseller_user_id=reseller_user.id,
        person_party_id=person.id,
        party_membership_id=membership.id,
        **_EVIDENCE,
    )
    original_evidence = (
        bound.party_bound_at,
        bound.party_binding_source,
        bound.party_binding_reason,
    )
    retried = party_service.bind_reseller_user_principal(
        db_session,
        reseller_user_id=reseller_user.id,
        person_party_id=person.id,
        party_membership_id=membership.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is bound
    assert retried.person_party_id == person.id
    assert retried.party_membership_id == membership.id
    assert (
        retried.party_bound_at,
        retried.party_binding_source,
        retried.party_binding_reason,
    ) == original_evidence
    assert retried.is_active is True
    assert membership.status == PartyMembershipStatus.invited.value
    assert db_session.query(PartyRole).count() == 0


def test_field_vendor_user_binds_to_explicit_person_and_vendor_context(db_session):
    person = _person(db_session)
    organization_party = _organization_party(db_session)
    system_user = _system_user(db_session)
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=system_user.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    vendor = Vendor(name="Private Vendor")
    db_session.add(vendor)
    db_session.flush()
    field_vendor = FieldVendor(
        name="Private Vendor",
        crm_vendor_id=str(vendor.id),
    )
    db_session.add(field_vendor)
    db_session.flush()
    field_vendor_user = FieldVendorUser(
        vendor_id=field_vendor.id,
        system_user_id=system_user.id,
        crm_vendor_user_id=str(uuid.uuid4()),
        role="technician",
        is_active=True,
    )
    db_session.add(field_vendor_user)
    db_session.flush()
    party_service.bind_vendor_profiles(
        db_session,
        vendor_id=vendor.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    membership = _membership(
        db_session,
        person=person,
        organization=organization_party,
        membership_type=PartyMembershipType.vendor_user,
    )

    auth_projection = party_service.bind_field_vendor_user_context(
        db_session,
        field_vendor_user_id=field_vendor_user.id,
        party_membership_id=membership.id,
        **_EVIDENCE,
    )
    original_evidence = (
        auth_projection.party_bound_at,
        auth_projection.party_binding_source,
        auth_projection.party_binding_reason,
    )
    retried = party_service.bind_field_vendor_user_context(
        db_session,
        field_vendor_user_id=field_vendor_user.id,
        party_membership_id=membership.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is auth_projection
    assert retried.party_membership_id == membership.id
    assert (
        retried.party_bound_at,
        retried.party_binding_source,
        retried.party_binding_reason,
    ) == original_evidence
    assert auth_projection.is_active is True
    assert system_user.is_active is True
    assert membership.status == PartyMembershipStatus.invited.value


def test_field_vendor_context_binding_rejects_repoint(db_session):
    person = _person(db_session)
    organization_party = _organization_party(db_session)
    system_user = _system_user(db_session)
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=system_user.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    vendor = Vendor(name="Private Vendor")
    db_session.add(vendor)
    db_session.flush()
    field_vendor = FieldVendor(name="Private Vendor", crm_vendor_id=str(vendor.id))
    db_session.add(field_vendor)
    db_session.flush()
    field_vendor_user = FieldVendorUser(
        vendor_id=field_vendor.id,
        system_user_id=system_user.id,
        crm_vendor_user_id=str(uuid.uuid4()),
    )
    db_session.add(field_vendor_user)
    db_session.flush()
    party_service.bind_vendor_profiles(
        db_session,
        vendor_id=vendor.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    membership = _membership(
        db_session,
        person=person,
        organization=organization_party,
        membership_type=PartyMembershipType.vendor_user,
    )
    party_service.bind_field_vendor_user_context(
        db_session,
        field_vendor_user_id=field_vendor_user.id,
        party_membership_id=membership.id,
        **_EVIDENCE,
    )
    second_membership = party_service.add_membership(
        db_session,
        person_party_id=person.id,
        organization_party_id=organization_party.id,
        membership_type=PartyMembershipType.vendor_user,
        membership_key="second-context",
    )

    with pytest.raises(party_service.PartyInvariantError, match="merge/repoint"):
        party_service.bind_field_vendor_user_context(
            db_session,
            field_vendor_user_id=field_vendor_user.id,
            party_membership_id=second_membership.id,
            **_EVIDENCE,
        )
    assert field_vendor_user.party_membership_id == membership.id


def test_organization_membership_binding_preserves_legacy_state(db_session):
    person = _person(db_session)
    organization_party = _organization_party(db_session)
    organization = Organization(name="Private Customer Organization")
    legacy_person_id = uuid.uuid4()
    projection = OrganizationMembership(
        organization=organization,
        person_id=legacy_person_id,
        role=OrganizationMembershipRole.admin.value,
        is_active=False,
    )
    db_session.add_all((organization, projection))
    db_session.flush()
    party_service.bind_organization_profile(
        db_session,
        organization_id=organization.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    membership = _membership(
        db_session,
        person=person,
        organization=organization_party,
        membership_type=PartyMembershipType.admin,
    )

    bound = party_service.bind_organization_membership_context(
        db_session,
        organization_membership_id=projection.id,
        party_membership_id=membership.id,
        **_EVIDENCE,
    )
    original_evidence = (
        bound.party_bound_at,
        bound.party_binding_source,
        bound.party_binding_reason,
    )
    retried = party_service.bind_organization_membership_context(
        db_session,
        organization_membership_id=projection.id,
        party_membership_id=membership.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is bound
    assert retried.party_membership_id == membership.id
    assert (
        retried.party_bound_at,
        retried.party_binding_source,
        retried.party_binding_reason,
    ) == original_evidence
    assert retried.person_id == legacy_person_id
    assert retried.role == OrganizationMembershipRole.admin.value
    assert retried.is_active is False
    assert membership.status == PartyMembershipStatus.invited.value


def test_model_constraints_reject_partial_principal_context_evidence(db_session):
    partial = ResellerUser(
        person_party_id=uuid.uuid4(),
        email="partial@example.test",
    )
    db_session.add(partial)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    table = PartyMembership.__table__
    assert table.name == "party_memberships"
