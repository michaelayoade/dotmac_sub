from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import support as support_api
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import team_inbox_read
from app.web.admin import inbox as admin_inbox


def _team(db_session, name: str, team_type: str = ServiceTeamType.support.value):
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(
    db_session,
    team: ServiceTeam,
    *,
    subject: str,
    status: str = InboxConversationStatus.open.value,
    channel_type: str = "email",
    contact_address: str = "customer@example.com",
    first_at: datetime | None = None,
) -> InboxConversation:
    first_at = first_at or datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = InboxConversation(
        channel_type=channel_type,
        status=status,
        subject=subject,
        contact_address=contact_address,
        primary_service_team_id=team.id,
        first_message_at=first_at,
        last_message_at=first_at,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            source=InboxTeamSource.routing_rule.value,
            is_active=True,
        )
    )
    db_session.flush()
    return conversation


def _message(
    db_session,
    conversation: InboxConversation,
    *,
    direction: str,
    body: str,
    at: datetime,
) -> InboxMessage:
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=direction,
        subject=conversation.subject,
        body=body,
        from_address="customer@example.com"
        if direction == InboxMessageDirection.inbound.value
        else "support@dotmac.io",
        to_addresses=["support@dotmac.io"]
        if direction == InboxMessageDirection.inbound.value
        else ["customer@example.com"],
        received_at=at if direction == InboxMessageDirection.inbound.value else None,
        sent_at=at if direction == InboxMessageDirection.outbound.value else None,
    )
    db_session.add(message)
    db_session.flush()
    return message


def test_list_conversations_filters_by_team_search_and_response_need(db_session):
    support = _team(db_session, "Support")
    billing = _team(db_session, "Billing", ServiceTeamType.billing.value)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    needs_reply = _conversation(
        db_session,
        support,
        subject="Router offline",
        contact_address="ada@example.com",
        first_at=base,
    )
    _message(
        db_session,
        needs_reply,
        direction=InboxMessageDirection.inbound.value,
        body="Router is down",
        at=base,
    )
    answered = _conversation(
        db_session,
        support,
        subject="Install date",
        contact_address="install@example.com",
        first_at=base,
    )
    _message(
        db_session,
        answered,
        direction=InboxMessageDirection.inbound.value,
        body="When is install?",
        at=base,
    )
    _message(
        db_session,
        answered,
        direction=InboxMessageDirection.outbound.value,
        body="Tomorrow.",
        at=base.replace(hour=9),
    )
    billing_case = _conversation(
        db_session,
        billing,
        subject="Invoice copy",
        contact_address="billing@example.com",
        first_at=base,
    )
    _message(
        db_session,
        billing_case,
        direction=InboxMessageDirection.inbound.value,
        body="Need invoice",
        at=base,
    )
    db_session.commit()

    result = team_inbox_read.list_conversations(
        db_session,
        service_team_id=support.id,
        search="router",
        needs_response=True,
    )

    assert result.count == 1
    assert [item.id for item in result.items] == [str(needs_reply.id)]
    assert result.items[0].primary_service_team_name == "Support"
    assert result.items[0].latest_message_body == "Router is down"
    assert result.items[0].needs_response is True


def test_list_conversations_api_returns_list_response(db_session):
    support = _team(db_session, "Support")
    conversation = _conversation(db_session, support, subject="Router offline")
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Down",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    db_session.commit()

    response = support_api.list_inbox_conversations(
        search=None,
        status=None,
        channel_type=None,
        service_team_id=str(support.id),
        assigned_person_id=None,
        needs_response=True,
        limit=50,
        offset=0,
        db=db_session,
    )

    assert response["count"] == 1
    assert response["items"][0].id == conversation.id
    assert response["items"][0].needs_response is True


def test_admin_inbox_queue_renders_filtered_context(db_session, monkeypatch):
    support = _team(db_session, "Support")
    conversation = _conversation(db_session, support, subject="Router offline")
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        body="Down",
        at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    captured: dict[str, object] = {}

    def _fake_template_response(template_name, context):
        captured["template_name"] = template_name
        captured["context"] = context
        return context

    monkeypatch.setattr(
        admin_inbox.templates,
        "TemplateResponse",
        _fake_template_response,
    )
    db_session.commit()

    context = admin_inbox.team_inbox_queue(
        request=SimpleNamespace(state=SimpleNamespace()),
        search="router",
        status=None,
        channel_type=None,
        service_team_id=str(support.id),
        assigned_person_id=None,
        needs_response=True,
        page=1,
        per_page=25,
        db=db_session,
    )

    assert captured["template_name"] == "admin/inbox/index.html"
    assert context["rows"][0].id == str(conversation.id)
    assert context["search"] == "router"
    assert context["service_team_id"] == str(support.id)
    assert context["needs_response"] is True


def test_conversation_timeline_returns_teams_assignments_and_messages(db_session):
    team = _team(db_session, "Support")
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        subject="Need help",
        contact_address="customer@example.com",
        primary_service_team_id=team.id,
        first_message_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        last_message_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationTeam(
            conversation_id=conversation.id,
            service_team_id=team.id,
            role=InboxTeamRole.owner.value,
            source=InboxTeamSource.routing_rule.value,
            is_active=True,
        )
    )
    assignee_id = uuid4()
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=assignee_id,
            assigned_at=datetime(2026, 7, 10, 8, 2, tzinfo=UTC),
            is_active=True,
        )
    )
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction=InboxMessageDirection.inbound.value,
                subject="Need help",
                body="Router offline",
                from_address="customer@example.com",
                to_addresses=["support@dotmac.io"],
                received_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type="email",
                direction=InboxMessageDirection.outbound.value,
                subject="Re: Need help",
                body="Checking.",
                from_address="support@dotmac.io",
                to_addresses=["customer@example.com"],
                sent_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
            ),
        ]
    )
    db_session.commit()

    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    assert timeline is not None
    assert timeline.id == str(conversation.id)
    assert timeline.primary_service_team_id == str(team.id)
    assert timeline.teams[0].service_team_name == "Support"
    assert timeline.assignments[0].person_id == str(assignee_id)
    assert [message.direction for message in timeline.messages] == [
        InboxMessageDirection.inbound.value,
        InboxMessageDirection.outbound.value,
    ]


def test_conversation_timeline_api_returns_404_for_inactive_conversation(db_session):
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        is_active=False,
    )
    db_session.add(conversation)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        support_api.get_inbox_conversation_timeline(conversation.id, db=db_session)

    assert exc.value.status_code == 404
