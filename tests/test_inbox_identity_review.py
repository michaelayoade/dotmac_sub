"""Exact Inbox sender review and administrator correction behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.db import finish_read_transaction
from app.models.party import Party, PartyContactPoint, PartyType
from app.models.rbac import Role, SystemUserRole
from app.models.sales import Lead
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.models.team_inbox import InboxConversation
from app.services import (
    conversation_lead_relationships,
    inbox_lead_actions,
    team_inbox_contact_links,
    team_inbox_customer_completion,
)
from app.services.owner_commands import CommandContext


def _staff(db_session, *, admin: bool) -> SystemUser:
    user = SystemUser(
        first_name="Ada",
        last_name="Reviewer",
        email=f"inbox-review-{uuid4()}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    if admin:
        role = db_session.query(Role).filter(Role.name == "admin").first()
        if role is None:
            role = Role(name="admin", is_active=True)
            db_session.add(role)
            db_session.flush()
        db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()
    return user


def _conflict(db_session):
    endpoint = f"inbox-review-{uuid4()}@example.com"
    conversation = InboxConversation(
        channel_type="email",
        contact_address=endpoint,
        subject="New service enquiry",
        is_active=True,
    )
    db_session.add(conversation)
    parties = []
    points = []
    for name in ("Old Owner One", "Old Owner Two"):
        party = Party(party_type=PartyType.person.value, display_name=name)
        db_session.add(party)
        db_session.flush()
        point = PartyContactPoint(
            party_id=party.id,
            channel_type="email",
            normalized_value=endpoint,
            display_value=endpoint,
            is_active=True,
        )
        db_session.add(point)
        parties.append(party)
        points.append(point)
    db_session.commit()
    return conversation, tuple(parties), tuple(points)


def _context(user_id: UUID, scope: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{user_id}",
        scope=scope,
        reason="Reviewed Inbox sender identity",
    )


def _review(db_session, conversation: InboxConversation):
    return team_inbox_contact_links.identity_review(
        db_session,
        conversation.id,
        can_read_customers=True,
        can_read_leads=True,
    )


def test_review_shows_exact_matches_and_refuses_stale_correction(db_session):
    conversation, parties, _ = _conflict(db_session)
    customer = Subscriber(
        party_id=parties[0].id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Existing Customer fixture",
        first_name="Old",
        last_name="Owner",
        email=conversation.contact_address,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(customer)
    db_session.commit()
    user = _staff(db_session, admin=False)
    conversation_id = conversation.id
    user_id = user.id
    review = _review(db_session, conversation)
    assert review.disposition.value == "ambiguous_match"
    assert {row.party_id for row in review.matches} == {p.id for p in parties}
    assert all(row.reason == "Exact sender email address" for row in review.matches)
    assert (
        next(row for row in review.matches if row.party_id == parties[0].id).customer_id
        == customer.id
    )

    finish_read_transaction(db_session)
    with pytest.raises(team_inbox_contact_links.ContactLinkError, match="changed"):
        team_inbox_contact_links.request_identity_correction(
            db_session,
            team_inbox_contact_links.RequestIdentityCorrectionCommand(
                context=_context(user_id, "inbox:identity-correction-request"),
                conversation_id=conversation_id,
                actor_system_user_id=user_id,
                expected_evidence_digest="stale",
                reason="Neither old owner is the sender.",
            ),
        )
    assert team_inbox_contact_links.identity_correction_status(conversation) is None


def test_only_administrator_can_clear_after_all_wrong_matches_are_removed(db_session):
    conversation, _, points = _conflict(db_session)
    agent = _staff(db_session, admin=False)
    admin = _staff(db_session, admin=True)
    conversation_id = conversation.id
    point_ids = tuple(point.id for point in points)
    agent_id = agent.id
    admin_id = admin.id
    review = _review(db_session, conversation)
    finish_read_transaction(db_session)
    requested = team_inbox_contact_links.request_identity_correction(
        db_session,
        team_inbox_contact_links.RequestIdentityCorrectionCommand(
            context=_context(agent_id, "inbox:identity-correction-request"),
            conversation_id=conversation_id,
            actor_system_user_id=agent_id,
            expected_evidence_digest=review.evidence_digest,
            reason="Both old records use a reassigned address.",
        ),
    )
    assert requested.status is team_inbox_contact_links.IdentityCorrectionStatus.open
    assert any(
        item.conversation_id == conversation_id
        for item in team_inbox_contact_links.open_identity_corrections(db_session)
    )
    finish_read_transaction(db_session)
    with pytest.raises(
        team_inbox_contact_links.ContactLinkError, match="administrator"
    ):
        team_inbox_contact_links.clear_identity_correction(
            db_session,
            team_inbox_contact_links.ClearIdentityCorrectionCommand(
                context=_context(agent_id, "inbox:identity-correction-clear"),
                conversation_id=conversation_id,
                actor_system_user_id=agent_id,
                reason="Verified address was reassigned.",
            ),
        )
    finish_read_transaction(db_session)
    with pytest.raises(
        team_inbox_contact_links.ContactLinkError, match="still matches"
    ):
        team_inbox_contact_links.clear_identity_correction(
            db_session,
            team_inbox_contact_links.ClearIdentityCorrectionCommand(
                context=_context(admin_id, "inbox:identity-correction-clear"),
                conversation_id=conversation_id,
                actor_system_user_id=admin_id,
                reason="Verified address was reassigned.",
            ),
        )
    for point_id in point_ids:
        review = _review(db_session, conversation)
        finish_read_transaction(db_session)
        team_inbox_contact_links.remove_incorrect_party_contact(
            db_session,
            team_inbox_contact_links.RemoveIncorrectPartyContactCommand(
                context=_context(admin_id, "inbox:identity-correction"),
                conversation_id=conversation_id,
                contact_point_id=point_id,
                actor_system_user_id=admin_id,
                expected_evidence_digest=review.evidence_digest,
                reason="This old Party no longer owns the address.",
            ),
        )
    action = inbox_lead_actions.resolve_action(
        db_session,
        conversation_id=conversation_id,
        intent=inbox_lead_actions.InboxActionIntent.lead,
        permissions=inbox_lead_actions.InboxActionPermissions(
            can_read_profile=True,
            can_edit_profile=True,
            can_read_leads=True,
            can_write_leads=True,
        ),
    )
    assert (
        action.action_type
        is inbox_lead_actions.InboxResolvedActionType.identity_review_required
    )
    finish_read_transaction(db_session)
    team_inbox_contact_links.clear_identity_correction(
        db_session,
        team_inbox_contact_links.ClearIdentityCorrectionCommand(
            context=_context(admin_id, "inbox:identity-correction-clear"),
            conversation_id=conversation_id,
            actor_system_user_id=admin_id,
            reason="Both wrong contact claims were removed.",
        ),
    )
    action = inbox_lead_actions.resolve_action(
        db_session,
        conversation_id=conversation_id,
        intent=inbox_lead_actions.InboxActionIntent.lead,
        permissions=inbox_lead_actions.InboxActionPermissions(
            can_read_profile=True,
            can_edit_profile=True,
            can_read_leads=True,
            can_write_leads=True,
        ),
    )
    assert (
        action.action_type
        is inbox_lead_actions.InboxResolvedActionType.create_party_and_lead
    )


def test_reviewed_existing_lead_is_linked_and_enables_lead_classification(db_session):
    conversation, parties, _ = _conflict(db_session)
    user = _staff(db_session, admin=False)
    lead = Lead(
        party_id=parties[0].id,
        title="Existing prospect",
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Existing reviewed prospect fixture",
        status="new",
        is_active=True,
    )
    db_session.add(lead)
    db_session.commit()
    conversation_id = conversation.id
    user_id = user.id
    lead_id = lead.id
    review = _review(db_session, conversation)
    assert any(lead.id in {item.id for item in row.leads} for row in review.matches)
    finish_read_transaction(db_session)
    outcome = inbox_lead_actions.link_reviewed_lead(
        db_session,
        inbox_lead_actions.LinkReviewedLeadCommand(
            context=_context(user_id, "inbox:reviewed-lead-selection"),
            conversation_id=conversation_id,
            lead_id=lead_id,
            expected_evidence_digest=review.evidence_digest,
            actor_system_user_id=user_id,
            actor_person_id=None,
        ),
    )
    assert outcome.lead_id == lead.id
    assert conversation_lead_relationships.active_link(db_session, conversation.id)
    assert (
        team_inbox_customer_completion.classification(db_session, conversation).value
        == "lead"
    )
