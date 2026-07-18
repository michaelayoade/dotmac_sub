from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.party import (
    PartyContactConsentStatus,
    PartyContactPoint,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyRelationship,
    PartyRelationshipType,
    PartyType,
    SubscriberContactPointProjection,
    SubscriberContactRelationshipProjection,
)
from app.models.subscriber import Subscriber, SubscriberContact, SubscriberStatus
from app.models.team_inbox import InboxChannelType, InboxContactLink
from app.services import party as party_service
from app.services import team_inbox_contact_links

_EVIDENCE = {
    "source": "reviewed_contact_projection_worklist",
    "reason": "Reviewed contact identity and routing context",
}


def _subscriber(db_session, *, account_party_type=PartyType.organization):
    account_party = party_service.create_party(
        db_session,
        party_type=account_party_type,
        display_name="Private Account Party",
    )
    subscriber = Subscriber(
        first_name="Private",
        last_name="Account",
        email="account@example.test",
        phone="+2348030000100",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=account_party.id,
        **_EVIDENCE,
    )
    return subscriber, account_party


def _contact(db_session, subscriber):
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        full_name="Private Linked Contact",
        email="linked@example.test",
        phone="+2348030000111",
        whatsapp="+2348030000111",
        facebook="private.handle",
        contact_type="billing",
        is_billing_contact=True,
        is_authorized=True,
        receives_notifications=True,
    )
    db_session.add(contact)
    db_session.flush()
    return contact


def _bound_contact(db_session):
    subscriber, account_party = _subscriber(db_session)
    contact = _contact(db_session, subscriber)
    person = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Private Linked Contact",
    )
    party_service.bind_subscriber_contact_person(
        db_session,
        subscriber_contact_id=contact.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    return subscriber, account_party, contact, person


def test_subscriber_contact_person_binding_is_identity_only_and_idempotent(db_session):
    subscriber, _account_party = _subscriber(db_session)
    contact = _contact(db_session, subscriber)
    person = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Private Linked Contact",
    )

    bound = party_service.bind_subscriber_contact_person(
        db_session,
        subscriber_contact_id=contact.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    original = (
        bound.party_bound_at,
        bound.party_binding_source,
        bound.party_binding_reason,
    )
    retried = party_service.bind_subscriber_contact_person(
        db_session,
        subscriber_contact_id=contact.id,
        person_party_id=person.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is bound
    assert retried.person_party_id == person.id
    assert (
        retried.party_bound_at,
        retried.party_binding_source,
        retried.party_binding_reason,
    ) == original
    assert retried.email == "linked@example.test"
    assert retried.is_authorized is True
    assert retried.is_billing_contact is True
    assert retried.receives_notifications is True
    assert db_session.query(PartyRelationship).count() == 0
    assert db_session.query(PartyContactPoint).count() == 0


def test_contact_person_binding_rejects_duplicate_and_repoint(db_session):
    subscriber, _account_party = _subscriber(db_session)
    first = _contact(db_session, subscriber)
    second = SubscriberContact(
        subscriber_id=subscriber.id,
        full_name="Duplicate Row",
        email="duplicate@example.test",
    )
    db_session.add(second)
    db_session.flush()
    first_person = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="First Person"
    )
    second_person = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Second Person"
    )
    party_service.bind_subscriber_contact_person(
        db_session,
        subscriber_contact_id=first.id,
        person_party_id=first_person.id,
        **_EVIDENCE,
    )

    with pytest.raises(party_service.PartyInvariantError, match="already bound"):
        party_service.bind_subscriber_contact_person(
            db_session,
            subscriber_contact_id=second.id,
            person_party_id=first_person.id,
            **_EVIDENCE,
        )
    with pytest.raises(party_service.PartyInvariantError, match="merge/repoint"):
        party_service.bind_subscriber_contact_person(
            db_session,
            subscriber_contact_id=first.id,
            person_party_id=second_person.id,
            **_EVIDENCE,
        )


def test_contact_person_binding_rejects_archived_subscriber_party(db_session):
    subscriber, account_party = _subscriber(db_session)
    contact = _contact(db_session, subscriber)
    person = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Private Linked Contact",
    )
    account_party.status = "archived"
    db_session.flush()

    with pytest.raises(
        party_service.PartyInvariantError,
        match="cannot receive a contact binding",
    ):
        party_service.bind_subscriber_contact_person(
            db_session,
            subscriber_contact_id=contact.id,
            person_party_id=person.id,
            **_EVIDENCE,
        )


def test_relationship_projection_requires_exact_person_and_account_parties(db_session):
    _subscriber_row, account_party, contact, person = _bound_contact(db_session)
    relationship = party_service.relate_parties(
        db_session,
        subject_party_id=person.id,
        object_party_id=account_party.id,
        relationship_type=PartyRelationshipType.billing_contact_for,
        source="reviewed_contact_relationship",
    )

    projection = party_service.bind_subscriber_contact_relationship(
        db_session,
        subscriber_contact_id=contact.id,
        party_relationship_id=relationship.id,
        **_EVIDENCE,
    )
    retried = party_service.bind_subscriber_contact_relationship(
        db_session,
        subscriber_contact_id=contact.id,
        party_relationship_id=relationship.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is projection
    assert projection.party_relationship_id == relationship.id
    assert relationship.relationship_type == "billing_contact_for"
    assert contact.is_authorized is True
    assert db_session.query(SubscriberContactRelationshipProjection).count() == 1

    wrong_target = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Wrong Target",
    )
    wrong_relationship = party_service.relate_parties(
        db_session,
        subject_party_id=person.id,
        object_party_id=wrong_target.id,
        relationship_type=PartyRelationshipType.contact_for,
    )
    with pytest.raises(party_service.PartyInvariantError, match="Subscriber Party"):
        party_service.bind_subscriber_contact_relationship(
            db_session,
            subscriber_contact_id=contact.id,
            party_relationship_id=wrong_relationship.id,
            **_EVIDENCE,
        )


def test_contact_point_projection_preserves_verification_and_consent(db_session):
    _subscriber_row, _account_party, contact, person = _bound_contact(db_session)
    point = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type=PartyContactPointType.email,
        normalized_value="linked@example.test",
        verification_status=PartyContactVerificationStatus.verified,
        consent_status=PartyContactConsentStatus.opted_out,
    )

    projection = party_service.bind_subscriber_contact_point(
        db_session,
        subscriber_contact_id=contact.id,
        source_field="email",
        party_contact_point_id=point.id,
        **_EVIDENCE,
    )
    retried = party_service.bind_subscriber_contact_point(
        db_session,
        subscriber_contact_id=contact.id,
        source_field="email",
        party_contact_point_id=point.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is projection
    assert projection.source_field == "email"
    assert point.verification_status == PartyContactVerificationStatus.verified.value
    assert point.consent_status == PartyContactConsentStatus.opted_out.value
    assert contact.receives_notifications is True
    assert db_session.query(SubscriberContactPointProjection).count() == 1


def test_social_projection_requires_scoped_canonical_identity(db_session):
    _subscriber_row, _account_party, contact, person = _bound_contact(db_session)
    point = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type=PartyContactPointType.facebook_messenger,
        normalized_value="psid-123",
        display_value="private.handle",
        provider="meta",
        provider_account_id="page-456",
        external_subject_id="psid-123",
    )

    projection = party_service.bind_subscriber_contact_point(
        db_session,
        subscriber_contact_id=contact.id,
        source_field="facebook",
        party_contact_point_id=point.id,
        **_EVIDENCE,
    )

    assert projection.party_contact_point_id == point.id


def test_inbox_projection_accepts_related_contact_without_changing_route(db_session):
    subscriber, account_party, contact, person = _bound_contact(db_session)
    party_service.relate_parties(
        db_session,
        subject_party_id=person.id,
        object_party_id=account_party.id,
        relationship_type=PartyRelationshipType.contact_for,
    )
    point = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type=PartyContactPointType.facebook_messenger,
        normalized_value="psid-123",
        provider="meta",
        provider_account_id="page-456",
        external_subject_id="psid-123",
    )
    link = InboxContactLink(
        channel_type=InboxChannelType.facebook_messenger.value,
        normalized_contact="psid-123",
        subscriber_id=subscriber.id,
        source="manual_inbox_conversation",
        is_active=True,
    )
    db_session.add(link)
    db_session.flush()

    bound = team_inbox_contact_links.bind_contact_link_party_contact_point(
        db_session,
        contact_link_id=link.id,
        party_contact_point_id=point.id,
        **_EVIDENCE,
    )
    original = (
        bound.party_contact_point_bound_at,
        bound.party_contact_point_binding_source,
        bound.party_contact_point_binding_reason,
    )
    retried = team_inbox_contact_links.bind_contact_link_party_contact_point(
        db_session,
        contact_link_id=link.id,
        party_contact_point_id=point.id,
        source="ignored_retry_source",
        reason="Ignored retry reason",
    )

    assert retried is bound
    assert retried.party_contact_point_id == point.id
    assert (
        retried.party_contact_point_bound_at,
        retried.party_contact_point_binding_source,
        retried.party_contact_point_binding_reason,
    ) == original
    assert retried.subscriber_id == subscriber.id
    assert retried.is_active is True
    assert retried.source == "manual_inbox_conversation"
    assert contact.is_authorized is True


def test_inbox_projection_rejects_unrelated_person_and_unsupported_channel(db_session):
    subscriber, _account_party = _subscriber(db_session)
    person = party_service.create_party(
        db_session, party_type=PartyType.person, display_name="Unrelated Person"
    )
    point = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type=PartyContactPointType.email,
        normalized_value="unrelated@example.test",
    )
    link = InboxContactLink(
        channel_type=InboxChannelType.email.value,
        normalized_contact="unrelated@example.test",
        subscriber_id=subscriber.id,
        source="manual_inbox_conversation",
    )
    db_session.add(link)
    db_session.flush()

    with pytest.raises(
        team_inbox_contact_links.ContactLinkError,
        match="no active contact relationship",
    ):
        team_inbox_contact_links.bind_contact_link_party_contact_point(
            db_session,
            contact_link_id=link.id,
            party_contact_point_id=point.id,
            **_EVIDENCE,
        )

    link.channel_type = InboxChannelType.chat_widget.value
    with pytest.raises(team_inbox_contact_links.ContactLinkError, match="no canonical"):
        team_inbox_contact_links.bind_contact_link_party_contact_point(
            db_session,
            contact_link_id=link.id,
            party_contact_point_id=point.id,
            **_EVIDENCE,
        )


def test_inbox_projection_rejects_archived_target_party(db_session):
    subscriber, account_party, _contact, person = _bound_contact(db_session)
    party_service.relate_parties(
        db_session,
        subject_party_id=person.id,
        object_party_id=account_party.id,
        relationship_type=PartyRelationshipType.contact_for,
    )
    point = party_service.add_contact_point(
        db_session,
        party_id=person.id,
        channel_type=PartyContactPointType.email,
        normalized_value="linked@example.test",
    )
    link = InboxContactLink(
        channel_type=InboxChannelType.email.value,
        normalized_contact="linked@example.test",
        subscriber_id=subscriber.id,
        source="manual_inbox_conversation",
    )
    db_session.add(link)
    db_session.flush()
    account_party.status = "archived"
    db_session.flush()

    with pytest.raises(
        team_inbox_contact_links.ContactLinkError,
        match="target has no routable Party",
    ):
        team_inbox_contact_links.bind_contact_link_party_contact_point(
            db_session,
            contact_link_id=link.id,
            party_contact_point_id=point.id,
            **_EVIDENCE,
        )


def test_constraints_reject_partial_contact_and_inbox_evidence(db_session):
    subscriber, _account_party = _subscriber(db_session)
    partial = SubscriberContact(
        subscriber_id=subscriber.id,
        full_name="Partial",
        person_party_id=uuid.uuid4(),
    )
    db_session.add(partial)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_constraints_reject_partial_inbox_contact_point_evidence(db_session):
    subscriber, account_party = _subscriber(db_session)
    point = party_service.add_contact_point(
        db_session,
        party_id=account_party.id,
        channel_type=PartyContactPointType.email,
        normalized_value="account@example.test",
    )
    partial = InboxContactLink(
        channel_type=InboxChannelType.email.value,
        normalized_contact="account@example.test",
        subscriber_id=subscriber.id,
        party_contact_point_id=point.id,
        source="manual_inbox_conversation",
    )
    db_session.add(partial)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
