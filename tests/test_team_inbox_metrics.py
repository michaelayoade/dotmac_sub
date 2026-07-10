from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
from app.services import team_inbox_metrics


def _team(db_session, name: str = "Support") -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _conversation(
    db_session, team: ServiceTeam, *, first_at: datetime
) -> InboxConversation:
    conversation = InboxConversation(
        channel_type="email",
        status=InboxConversationStatus.open.value,
        subject="Need help",
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
    at: datetime,
) -> InboxMessage:
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="email",
        direction=direction,
        subject="Message",
        body="Body",
        received_at=at if direction == InboxMessageDirection.inbound.value else None,
        sent_at=at if direction == InboxMessageDirection.outbound.value else None,
    )
    db_session.add(message)
    db_session.flush()
    return message


def test_team_performance_metrics_tracks_response_and_queue_wait(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.outbound.value,
        at=base + timedelta(minutes=12),
    )
    person_id = uuid4()
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person_id,
            assigned_at=base + timedelta(minutes=4),
            is_active=True,
        )
    )
    db_session.commit()

    metrics = team_inbox_metrics.team_performance_metrics(
        db_session,
        team.id,
        response_sla_seconds=900,
        now=base + timedelta(hours=1),
    )

    assert metrics.conversation_count == 1
    assert metrics.open_count == 1
    assert metrics.assigned_open_count == 1
    assert metrics.unassigned_open_count == 0
    assert metrics.inbound_message_count == 1
    assert metrics.outbound_message_count == 1
    assert metrics.responded_count == 1
    assert metrics.response_sla_breached_count == 0
    assert metrics.average_first_response_seconds == 720
    assert metrics.average_queue_wait_seconds == 240


def test_team_performance_metrics_counts_pending_response_breach(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    metrics = team_inbox_metrics.team_performance_metrics(
        db_session,
        team.id,
        response_sla_seconds=600,
        now=base + timedelta(minutes=20),
    )

    assert metrics.open_count == 1
    assert metrics.unassigned_open_count == 1
    assert metrics.responded_count == 0
    assert metrics.response_sla_breached_count == 1
    assert metrics.average_first_response_seconds is None


def test_resolved_conversation_is_not_open(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    conversation = _conversation(db_session, team, first_at=base)
    conversation.status = InboxConversationStatus.resolved.value
    _message(
        db_session,
        conversation,
        direction=InboxMessageDirection.inbound.value,
        at=base,
    )
    db_session.commit()

    metrics = team_inbox_metrics.team_performance_metrics(db_session, team.id)

    assert metrics.conversation_count == 1
    assert metrics.open_count == 0
    assert metrics.unassigned_open_count == 0


def test_agent_performance_metrics_tracks_active_assignments_and_wait(db_session):
    team = _team(db_session)
    base = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    person_id = uuid4()
    conversation = _conversation(db_session, team, first_at=base)
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=team.id,
            person_id=person_id,
            assigned_at=base + timedelta(minutes=5),
            is_active=True,
        )
    )
    db_session.commit()

    metrics = team_inbox_metrics.agent_performance_metrics(
        db_session,
        service_team_id=team.id,
        person_id=person_id,
    )

    assert metrics.active_assignment_count == 1
    assert metrics.handled_conversation_count == 1
    assert metrics.average_queue_wait_seconds == 300
