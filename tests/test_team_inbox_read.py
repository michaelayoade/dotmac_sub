from __future__ import annotations

from datetime import UTC, datetime
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


def test_conversation_timeline_returns_teams_assignments_and_messages(db_session):
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
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
