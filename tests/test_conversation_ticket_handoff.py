"""Behaviour of communications.conversation_ticket_handoff.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 3.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import conversation_ticket_handoff as handoff


@pytest.fixture()
def customer(db_session):
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Inbox",
        last_name="Customer",
        email=f"handoff-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _conversation(db_session, *, subscriber_id=None, status=None):
    conversation = InboxConversation(
        channel_type="whatsapp",
        subject="No internet since morning",
        contact_address="+2348000000000",
        status=status or InboxConversationStatus.open.value,
        subscriber_id=subscriber_id,
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)
    return conversation


def _command(conversation, **overrides):
    base = dict(
        conversation_id=conversation.id,
        actor_id=uuid.uuid4(),
        actor_type=handoff.HandoffActorType.SYSTEM_USER,
        permission_keys=frozenset({"support:ticket:update"}),
        title="No internet since morning",
        description="Customer reports total outage.",
        reason="issued from inbox",
    )
    base.update(overrides)
    return handoff.ConversationTicketIssueCommand(**base)


def test_issue_records_provenance_and_inherits_customer(db_session, customer):
    conversation = _conversation(db_session, subscriber_id=customer.id)

    result = handoff.issue_ticket(db_session, _command(conversation))

    assert result.replayed is False
    assert result.ticket.origin_conversation_id == conversation.id
    assert result.ticket.subscriber_id == customer.id
    assert result.ticket.number


def test_reissuing_the_same_intent_replays(db_session, customer):
    """A double-submitted form must not open a second ticket."""
    conversation = _conversation(db_session, subscriber_id=customer.id)
    actor = uuid.uuid4()

    first = handoff.issue_ticket(db_session, _command(conversation, actor_id=actor))
    second = handoff.issue_ticket(db_session, _command(conversation, actor_id=actor))

    assert second.replayed is True
    assert second.ticket.id == first.ticket.id
    assert db_session.query(Ticket).filter(
        Ticket.origin_conversation_id == conversation.id
    ).count() == 1


def test_a_different_intent_opens_a_second_ticket(db_session, customer):
    """One conversation may legitimately issue many tickets."""
    conversation = _conversation(db_session, subscriber_id=customer.id)
    actor = uuid.uuid4()

    handoff.issue_ticket(db_session, _command(conversation, actor_id=actor))
    handoff.issue_ticket(
        db_session,
        _command(conversation, actor_id=actor, title="Separate billing dispute"),
    )

    assert (
        db_session.query(Ticket)
        .filter(Ticket.origin_conversation_id == conversation.id)
        .count()
        == 2
    )


def test_issuance_does_not_transition_the_conversation(db_session, customer):
    """Opening a ticket and resolving a thread are separate decisions."""
    conversation = _conversation(db_session, subscriber_id=customer.id)

    handoff.issue_ticket(db_session, _command(conversation))

    db_session.refresh(conversation)
    assert conversation.status == InboxConversationStatus.open.value


def test_missing_permission_is_refused(db_session, customer):
    conversation = _conversation(db_session, subscriber_id=customer.id)

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(
            db_session, _command(conversation, permission_keys=frozenset())
        )
    assert exc.value.kind == "forbidden"


def test_resolved_conversation_cannot_issue(db_session, customer):
    conversation = _conversation(
        db_session,
        subscriber_id=customer.id,
        status=InboxConversationStatus.resolved.value,
    )

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(db_session, _command(conversation))
    assert exc.value.kind == "conflict"


def test_blank_title_is_refused(db_session, customer):
    conversation = _conversation(db_session, subscriber_id=customer.id)

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(db_session, _command(conversation, title="   "))
    assert exc.value.kind == "invalid"


def test_unknown_conversation_is_not_found(db_session):
    conversation = _conversation(db_session)
    command = _command(conversation, conversation_id=uuid.uuid4())

    with pytest.raises(handoff.ConversationTicketHandoffError) as exc:
        handoff.issue_ticket(db_session, command)
    assert exc.value.kind == "not_found"


def test_list_for_conversation_returns_issued_tickets(db_session, customer):
    conversation = _conversation(db_session, subscriber_id=customer.id)
    result = handoff.issue_ticket(db_session, _command(conversation))

    listed = handoff.list_for_conversation(db_session, conversation.id)

    assert [row.id for row in listed] == [result.ticket.id]


def test_unresolved_conversation_still_issues_without_a_customer(db_session):
    """An unlinked thread can still raise a ticket; identity is simply absent."""
    conversation = _conversation(db_session)

    result = handoff.issue_ticket(db_session, _command(conversation))

    assert result.ticket.origin_conversation_id == conversation.id
    assert result.ticket.subscriber_id is None
