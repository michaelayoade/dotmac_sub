from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
)


@dataclass(frozen=True)
class InboxTeamPerformanceMetrics:
    service_team_id: str
    conversation_count: int
    open_count: int
    unassigned_open_count: int
    assigned_open_count: int
    inbound_message_count: int
    outbound_message_count: int
    responded_count: int
    response_sla_breached_count: int
    average_first_response_seconds: float | None
    average_queue_wait_seconds: float | None


@dataclass(frozen=True)
class InboxAgentPerformanceMetrics:
    person_id: str
    service_team_id: str
    active_assignment_count: int
    handled_conversation_count: int
    average_queue_wait_seconds: float | None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    if start_utc is None or end_utc is None:
        return None
    return max((end_utc - start_utc).total_seconds(), 0.0)


def _avg(values: list[float]) -> float | None:
    return mean(values) if values else None


def _conversation_ids_for_team(db: Session, service_team_id: str | UUID) -> list[UUID]:
    team_uuid = UUID(str(service_team_id))
    rows = (
        db.query(InboxConversationTeam.conversation_id)
        .filter(InboxConversationTeam.service_team_id == team_uuid)
        .filter(InboxConversationTeam.is_active.is_(True))
        .all()
    )
    return [row[0] for row in rows]


def _messages_by_conversation(
    db: Session,
    conversation_ids: list[UUID],
) -> dict[UUID, list[InboxMessage]]:
    if not conversation_ids:
        return {}
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id.in_(conversation_ids))
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    grouped: dict[UUID, list[InboxMessage]] = {}
    for message in messages:
        grouped.setdefault(message.conversation_id, []).append(message)
    return grouped


def _message_time(message: InboxMessage) -> datetime:
    return message.received_at or message.sent_at or message.created_at


def _first_response_seconds(messages: list[InboxMessage]) -> float | None:
    first_inbound = next(
        (
            message
            for message in messages
            if message.direction == InboxMessageDirection.inbound.value
        ),
        None,
    )
    if first_inbound is None:
        return None
    inbound_time = _message_time(first_inbound)
    first_outbound = next(
        (
            message
            for message in messages
            if message.direction == InboxMessageDirection.outbound.value
            and _message_time(message) >= inbound_time
        ),
        None,
    )
    if first_outbound is None:
        return None
    return _seconds_between(inbound_time, _message_time(first_outbound))


def team_performance_metrics(
    db: Session,
    service_team_id: str | UUID,
    *,
    response_sla_seconds: int | None = None,
    now: datetime | None = None,
) -> InboxTeamPerformanceMetrics:
    team_uuid = UUID(str(service_team_id))
    now_utc = _as_utc(now) or datetime.now(UTC)
    conversation_ids = _conversation_ids_for_team(db, team_uuid)
    conversations = (
        db.query(InboxConversation)
        .filter(InboxConversation.id.in_(conversation_ids))
        .all()
        if conversation_ids
        else []
    )
    messages_by_conversation = _messages_by_conversation(db, conversation_ids)
    active_assignments = (
        {
            row.conversation_id: row
            for row in db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id.in_(conversation_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        if conversation_ids
        else {}
    )

    inbound_count = 0
    outbound_count = 0
    response_values: list[float] = []
    queue_wait_values: list[float] = []
    response_breaches = 0

    for conversation in conversations:
        messages = messages_by_conversation.get(conversation.id, [])
        inbound_count += sum(
            1
            for message in messages
            if message.direction == InboxMessageDirection.inbound.value
        )
        outbound_count += sum(
            1
            for message in messages
            if message.direction == InboxMessageDirection.outbound.value
        )
        response_seconds = _first_response_seconds(messages)
        if response_seconds is not None:
            response_values.append(response_seconds)
            if (
                response_sla_seconds is not None
                and response_seconds > response_sla_seconds
            ):
                response_breaches += 1
        elif response_sla_seconds is not None:
            first_inbound = next(
                (
                    message
                    for message in messages
                    if message.direction == InboxMessageDirection.inbound.value
                ),
                None,
            )
            pending_seconds = (
                _seconds_between(_message_time(first_inbound), now_utc)
                if first_inbound is not None
                else None
            )
            if pending_seconds is not None and pending_seconds > response_sla_seconds:
                response_breaches += 1

        assignment = active_assignments.get(conversation.id)
        if assignment is not None:
            queue_wait = _seconds_between(
                conversation.first_message_at, assignment.assigned_at
            )
            if queue_wait is not None:
                queue_wait_values.append(queue_wait)

    open_conversations = [
        conversation
        for conversation in conversations
        if conversation.status != InboxConversationStatus.resolved.value
    ]
    assigned_open_count = sum(
        1
        for conversation in open_conversations
        if conversation.id in active_assignments
    )
    return InboxTeamPerformanceMetrics(
        service_team_id=str(team_uuid),
        conversation_count=len(conversations),
        open_count=len(open_conversations),
        unassigned_open_count=len(open_conversations) - assigned_open_count,
        assigned_open_count=assigned_open_count,
        inbound_message_count=inbound_count,
        outbound_message_count=outbound_count,
        responded_count=len(response_values),
        response_sla_breached_count=response_breaches,
        average_first_response_seconds=_avg(response_values),
        average_queue_wait_seconds=_avg(queue_wait_values),
    )


def agent_performance_metrics(
    db: Session,
    *,
    service_team_id: str | UUID,
    person_id: str | UUID,
) -> InboxAgentPerformanceMetrics:
    team_uuid = UUID(str(service_team_id))
    person_uuid = UUID(str(person_id))
    assignments = (
        db.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.service_team_id == team_uuid)
        .filter(InboxConversationAssignment.person_id == person_uuid)
        .all()
    )
    conversation_ids = [assignment.conversation_id for assignment in assignments]
    conversations = (
        {
            conversation.id: conversation
            for conversation in db.query(InboxConversation)
            .filter(InboxConversation.id.in_(conversation_ids))
            .all()
        }
        if conversation_ids
        else {}
    )
    queue_wait_values: list[float] = []
    for assignment in assignments:
        conversation = conversations.get(assignment.conversation_id)
        if conversation is None:
            continue
        queue_wait = _seconds_between(
            conversation.first_message_at, assignment.assigned_at
        )
        if queue_wait is not None:
            queue_wait_values.append(queue_wait)

    return InboxAgentPerformanceMetrics(
        person_id=str(person_uuid),
        service_team_id=str(team_uuid),
        active_assignment_count=sum(
            1 for assignment in assignments if assignment.is_active
        ),
        handled_conversation_count=len(
            {assignment.conversation_id for assignment in assignments}
        ),
        average_queue_wait_seconds=_avg(queue_wait_values),
    )
