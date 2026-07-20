from __future__ import annotations

import pytest

from app.models.party import (
    PartnerRoleKey,
    PartyContactPoint,
    PartyMembershipStatus,
    PartyMembershipType,
    PartyRelationshipType,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.services import party as party_service


def test_organization_can_hold_reseller_and_vendor_roles_independently(db_session):
    organization = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="ABC Networks Ltd",
    )

    reseller = party_service.assign_role(
        db_session,
        party_id=organization.id,
        role_type=PartyRoleType.reseller,
        status=PartyRoleStatus.active,
    )
    vendor = party_service.assign_role(
        db_session,
        party_id=organization.id,
        role_type=PartyRoleType.vendor,
        status=PartyRoleStatus.active,
    )
    party_service.transition_role(
        db_session, role_id=vendor.id, status=PartyRoleStatus.suspended
    )

    assert reseller.role_key == "default"
    assert reseller.status == PartyRoleStatus.active.value
    assert vendor.status == PartyRoleStatus.suspended.value


def test_reseller_and_partner_are_distinct_permissionless_contracts():
    reseller = party_service.role_contract(PartyRoleType.reseller)
    referral_partner = party_service.role_contract(
        PartyRoleType.partner, PartnerRoleKey.referral.value
    )

    assert reseller.role_type == "reseller"
    assert "managed_customer_scope" in reseller.capability_domains
    assert reseller.implicit_permissions == ()
    assert referral_partner.role_type == "partner"
    assert referral_partner.role_key == "referral"
    assert referral_partner.capability_domains == ("referrals",)
    assert referral_partner.implicit_permissions == ()

    with pytest.raises(
        party_service.PartyInvariantError, match="requires an explicit key"
    ):
        party_service.role_contract(PartyRoleType.partner)

    with pytest.raises(party_service.PartyInvariantError, match="not a partner alias"):
        party_service.role_contract(PartyRoleType.reseller, "referral")


def test_membership_requires_person_and_organization_parties(db_session):
    person = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Jane Doe"
    )
    organization = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="ABC Networks Ltd",
    )

    membership = party_service.add_membership(
        db_session,
        person_party_id=person.id,
        organization_party_id=organization.id,
        membership_type=PartyMembershipType.reseller_admin,
        status=PartyMembershipStatus.active,
        access_scope={"managed_subscriber_ids": []},
    )

    assert membership.person_party_id == person.id
    assert membership.organization_party_id == organization.id
    assert membership.access_scope == {"managed_subscriber_ids": []}

    with pytest.raises(
        party_service.PartyInvariantError,
        match="person_party_id must reference a person party",
    ):
        party_service.add_membership(
            db_session,
            person_party_id=organization.id,
            organization_party_id=person.id,
            membership_type=PartyMembershipType.vendor_user,
        )


def test_relationship_is_directional_and_cannot_target_self(db_session):
    person = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Jane Doe"
    )
    organization = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="ABC Networks Ltd",
    )

    relationship = party_service.relate_parties(
        db_session,
        subject_party_id=person.id,
        object_party_id=organization.id,
        relationship_type=PartyRelationshipType.billing_contact_for,
    )

    assert relationship.subject_party_id == person.id
    assert relationship.object_party_id == organization.id
    assert relationship.relationship_type == "billing_contact_for"

    with pytest.raises(
        party_service.PartyInvariantError, match="relationship with itself"
    ):
        party_service.relate_parties(
            db_session,
            subject_party_id=person.id,
            object_party_id=person.id,
            relationship_type=PartyRelationshipType.owner_of,
        )


def test_shared_email_is_not_a_global_identity_key(db_session):
    first = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Customer One"
    )
    second = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Customer Two"
    )

    first_contact = party_service.add_contact_point(
        db_session,
        party_id=first.id,
        channel_type="email",
        normalized_value="OWNER@EXAMPLE.COM",
        is_primary=True,
    )
    second_contact = party_service.add_contact_point(
        db_session,
        party_id=second.id,
        channel_type="email",
        normalized_value="owner@example.com",
        is_primary=True,
    )

    assert first_contact.normalized_value == "owner@example.com"
    assert second_contact.normalized_value == "owner@example.com"
    assert (
        db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.normalized_value == "owner@example.com")
        .count()
        == 2
    )


def test_social_contact_requires_scoped_immutable_subject(db_session):
    person = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Jane Doe"
    )

    with pytest.raises(
        party_service.PartyInvariantError,
        match="immutable external_subject_id",
    ):
        party_service.add_contact_point(
            db_session,
            party_id=person.id,
            channel_type="instagram_dm",
            normalized_value="@janedoe",
        )

    contact = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type="instagram_dm",
        normalized_value="17841400000000000",
        display_value="@janedoe",
        scope_key="page:123",
        provider="meta",
        provider_account_id="123",
        external_subject_id="17841400000000000",
    )

    assert contact.provider == "meta"
    assert contact.provider_account_id == "123"
    assert contact.external_subject_id == "17841400000000000"
