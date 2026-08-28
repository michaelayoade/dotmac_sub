from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, or_, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxStatusTransitionEvent,
)
from app.services import service_team_composition, team_inbox_assignment


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
    resolved_conversation_count: int
    average_first_response_seconds: float | None
    average_queue_wait_seconds: float | None


@dataclass(frozen=True)
class InboxTeamPerformanceReportRow:
    service_team_id: str
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    response_sla_seconds: int | None
    metrics: InboxTeamPerformanceMetrics


@dataclass(frozen=True)
class InboxAgentPerformanceReportRow:
    person_id: str
    agent_name: str
    service_team_id: str
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    metrics: InboxAgentPerformanceMetrics


class InboxAgentPerformanceQueryError(ValueError):
    """Stable validation failure for the bounded agent analytics query."""

    code = "ui.crm_operational_reports.invalid_query"


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceQuery:
    """Typed event-time, identity, search, and pagination boundary."""

    start_at: datetime
    end_at: datetime
    page: int = 1
    per_page: int | None = 50
    person_id: UUID | None = None
    search: str | None = None

    def __post_init__(self) -> None:
        start_at = _as_utc(self.start_at)
        end_at = _as_utc(self.end_at)
        if start_at is None or end_at is None or start_at >= end_at:
            raise InboxAgentPerformanceQueryError(
                "Agent performance requires an increasing bounded date range."
            )
        if self.page < 1:
            raise InboxAgentPerformanceQueryError("Page must be at least one.")
        if self.per_page is not None and not 10 <= self.per_page <= 500:
            raise InboxAgentPerformanceQueryError(
                "Page size must be between 10 and 500."
            )


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceAnalyticsRow:
    person_id: UUID
    agent_name: str
    service_team_id: UUID
    service_team_name: str
    assigned_conversation_count: int
    resolved_conversation_count: int
    active_assignment_count: int
    average_resolution_seconds: float | None
    average_first_response_seconds: float | None


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceSummary:
    agent_count: int
    assigned_conversation_count: int
    resolved_conversation_count: int
    active_assignment_count: int
    average_resolution_seconds: float | None
    average_first_response_seconds: float | None


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceAnalyticsPage:
    rows: tuple[InboxAgentPerformanceAnalyticsRow, ...]
    summary: InboxAgentPerformanceSummary
    total: int
    page: int
    per_page: int
    generated_at: datetime
    provenance: str = "live authoritative inbox events"

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.per_page < self.total


@dataclass(frozen=True)
class InboxEscalationCandidate:
    conversation_id: str
    service_team_id: str
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    subject: str | None
    contact_address: str | None
    status: str
    reasons: tuple[str, ...]
    response_sla_seconds: int | None
    queue_sla_seconds: int | None
    pending_response_seconds: float | None
    queue_wait_seconds: float | None
    assigned_person_id: str | None
    available_agent_count: int


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


def _positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _staff_display_name(user: SystemUser) -> str:
    """Project the canonical staff label without exposing the identity UUID."""

    return (
        (user.display_name or "").strip()
        or f"{user.first_name} {user.last_name}".strip()
        or user.email
    )


def response_sla_seconds_for_team(
    team: ServiceTeam,
    *,
    fallback: int | None = None,
) -> int | None:
    metadata = team.metadata_ or {}
    nested = metadata.get("inbox_sla")
    candidates = [
        metadata.get("inbox_response_sla_seconds"),
        metadata.get("response_sla_seconds"),
    ]
    if isinstance(nested, dict):
        candidates.extend(
            [
                nested.get("response_sla_seconds"),
                nested.get("first_response_seconds"),
            ]
        )
    for candidate in candidates:
        parsed = _positive_int(candidate)
        if parsed is not None:
            return parsed
    return fallback


def queue_sla_seconds_for_team(
    team: ServiceTeam,
    *,
    fallback: int | None = None,
) -> int | None:
    metadata = team.metadata_ or {}
    nested = metadata.get("inbox_sla")
    candidates = [
        metadata.get("inbox_queue_sla_seconds"),
        metadata.get("queue_sla_seconds"),
    ]
    if isinstance(nested, dict):
        candidates.extend(
            [
                nested.get("queue_sla_seconds"),
                nested.get("assignment_sla_seconds"),
                nested.get("assignment_seconds"),
            ]
        )
    for candidate in candidates:
        parsed = _positive_int(candidate)
        if parsed is not None:
            return parsed
    return fallback


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
    response = _first_response(messages)
    if response is None:
        return None
    first_inbound, first_outbound = response
    return _seconds_between(_message_time(first_inbound), _message_time(first_outbound))


def _first_response(
    messages: list[InboxMessage],
) -> tuple[InboxMessage, InboxMessage] | None:
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
    return first_inbound, first_outbound


def _sent_by_person_id(message: InboxMessage) -> UUID | None:
    """Return the recorded human sender for an Inbox outbound message."""

    metadata = message.metadata_ or {}
    raw_person_id = metadata.get("sent_by_person_id")
    if not raw_person_id:
        return None
    try:
        return UUID(str(raw_person_id))
    except (TypeError, ValueError):
        return None


def _first_human_response(
    messages: list[InboxMessage],
) -> tuple[InboxMessage, InboxMessage] | None:
    """Return the first agent-authored response to the first inbound message."""

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
            and _sent_by_person_id(message) is not None
        ),
        None,
    )
    if first_outbound is None:
        return None
    return first_inbound, first_outbound


def _first_inbound_message(messages: list[InboxMessage]) -> InboxMessage | None:
    return next(
        (
            message
            for message in messages
            if message.direction == InboxMessageDirection.inbound.value
        ),
        None,
    )


def _has_outbound_after(messages: list[InboxMessage], at: datetime) -> bool:
    return any(
        message.direction == InboxMessageDirection.outbound.value
        and _message_time(message) >= at
        for message in messages
    )


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

    first_response_values: list[float] = []
    team_conversation_ids = _conversation_ids_for_team(db, team_uuid)
    for messages in _messages_by_conversation(db, team_conversation_ids).values():
        response = _first_human_response(messages)
        if response is None:
            continue
        first_inbound, first_outbound = response
        if _sent_by_person_id(first_outbound) != person_uuid:
            continue
        response_seconds = _seconds_between(
            _message_time(first_inbound), _message_time(first_outbound)
        )
        if response_seconds is not None:
            first_response_values.append(response_seconds)

    return InboxAgentPerformanceMetrics(
        person_id=str(person_uuid),
        service_team_id=str(team_uuid),
        active_assignment_count=sum(
            1 for assignment in assignments if assignment.is_active
        ),
        handled_conversation_count=len(
            {assignment.conversation_id for assignment in assignments}
        ),
        resolved_conversation_count=len(
            {
                assignment.conversation_id
                for assignment in assignments
                if (conversation := conversations.get(assignment.conversation_id))
                is not None
                and conversation.status == InboxConversationStatus.resolved.value
            }
        ),
        average_first_response_seconds=_avg(first_response_values),
        average_queue_wait_seconds=_avg(queue_wait_values),
    )


def team_performance_report(
    db: Session,
    *,
    response_sla_seconds: int | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[InboxTeamPerformanceReportRow]:
    query = db.query(ServiceTeam).order_by(ServiceTeam.name.asc())
    if not include_inactive:
        query = query.filter(ServiceTeam.is_active.is_(True))
    teams = query.all()
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for team in teams),
    )
    rows: list[InboxTeamPerformanceReportRow] = []
    for team in teams:
        team_sla_seconds = response_sla_seconds_for_team(
            team,
            fallback=response_sla_seconds,
        )
        rows.append(
            InboxTeamPerformanceReportRow(
                service_team_id=str(team.id),
                service_team_name=team.name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team.id]
                ),
                response_sla_seconds=team_sla_seconds,
                metrics=team_performance_metrics(
                    db,
                    team.id,
                    response_sla_seconds=team_sla_seconds,
                    now=now,
                ),
            )
        )
    return rows


def agent_performance_report(
    db: Session,
    *,
    service_team_id: str | UUID | None = None,
    include_inactive_members: bool = False,
    search: str | None = None,
) -> list[InboxAgentPerformanceReportRow]:
    query = (
        db.query(ServiceTeamMember, ServiceTeam, SystemUser)
        .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .filter(ServiceTeam.is_active.is_(True))
        .filter(SystemUser.is_active.is_(True))
        .order_by(ServiceTeam.name.asc(), ServiceTeamMember.created_at.asc())
    )
    if service_team_id is not None:
        query = query.filter(ServiceTeam.id == UUID(str(service_team_id)))
    if not include_inactive_members:
        query = query.filter(ServiceTeamMember.is_active.is_(True))
    normalized_search = (search or "").strip()
    if normalized_search:
        search_term = f"%{normalized_search}%"
        query = query.filter(
            or_(
                SystemUser.display_name.ilike(search_term),
                SystemUser.first_name.ilike(search_term),
                SystemUser.last_name.ilike(search_term),
                SystemUser.email.ilike(search_term),
            )
        )

    members = query.all()
    if not members:
        return []

    team_ids = tuple({team.id for _member, team, _user in members})
    assignments = (
        db.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.service_team_id.in_(team_ids))
        .all()
    )
    assignments_by_agent: dict[
        tuple[UUID, UUID], list[InboxConversationAssignment]
    ] = {}
    for assignment in assignments:
        assignments_by_agent.setdefault(
            (assignment.service_team_id, assignment.person_id), []
        ).append(assignment)

    assignment_conversation_ids = {
        assignment.conversation_id for assignment in assignments
    }
    conversations = {
        conversation.id: conversation
        for conversation in (
            db.query(InboxConversation)
            .filter(InboxConversation.id.in_(assignment_conversation_ids))
            .all()
            if assignment_conversation_ids
            else []
        )
    }
    team_links = (
        db.query(InboxConversationTeam)
        .filter(InboxConversationTeam.service_team_id.in_(team_ids))
        .filter(InboxConversationTeam.is_active.is_(True))
        .all()
    )
    team_conversation_ids: dict[UUID, set[UUID]] = {}
    for link in team_links:
        team_conversation_ids.setdefault(link.service_team_id, set()).add(
            link.conversation_id
        )
    message_ids = {
        conversation_id
        for conversation_ids in team_conversation_ids.values()
        for conversation_id in conversation_ids
    }
    messages_by_conversation = _messages_by_conversation(db, list(message_ids))
    response_seconds_by_agent: dict[tuple[UUID, UUID], list[float]] = {}
    for team_id, conversation_ids in team_conversation_ids.items():
        for conversation_id in conversation_ids:
            response = _first_human_response(
                messages_by_conversation.get(conversation_id, [])
            )
            if response is None:
                continue
            first_inbound, first_outbound = response
            person_id = _sent_by_person_id(first_outbound)
            response_seconds = _seconds_between(
                _message_time(first_inbound), _message_time(first_outbound)
            )
            if person_id is not None and response_seconds is not None:
                response_seconds_by_agent.setdefault((team_id, person_id), []).append(
                    response_seconds
                )

    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        team_ids,
    )
    rows: list[InboxAgentPerformanceReportRow] = []
    for member, team, user in members:
        agent_assignments = assignments_by_agent.get((team.id, user.id), [])
        queue_wait_values = [
            queue_wait
            for assignment in agent_assignments
            if (conversation := conversations.get(assignment.conversation_id))
            is not None
            and (
                queue_wait := _seconds_between(
                    conversation.first_message_at, assignment.assigned_at
                )
            )
            is not None
        ]
        metrics = InboxAgentPerformanceMetrics(
            person_id=str(user.id),
            service_team_id=str(team.id),
            active_assignment_count=sum(
                1 for assignment in agent_assignments if assignment.is_active
            ),
            handled_conversation_count=len(
                {assignment.conversation_id for assignment in agent_assignments}
            ),
            resolved_conversation_count=len(
                {
                    assignment.conversation_id
                    for assignment in agent_assignments
                    if (conversation := conversations.get(assignment.conversation_id))
                    is not None
                    and conversation.status == InboxConversationStatus.resolved.value
                }
            ),
            average_first_response_seconds=_avg(
                response_seconds_by_agent.get((team.id, user.id), [])
            ),
            average_queue_wait_seconds=_avg(queue_wait_values),
        )
        rows.append(
            InboxAgentPerformanceReportRow(
                person_id=str(user.id),
                agent_name=_staff_display_name(user),
                service_team_id=str(team.id),
                service_team_name=team.name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team.id]
                ),
                metrics=metrics,
            )
        )
    return rows


def _analytics_duration_seconds(
    db: Session,
    *,
    started_at: ColumnElement[datetime],
    ended_at: ColumnElement[datetime],
) -> ColumnElement[float | None]:
    """Return a portable SQL expression for a non-negative duration."""

    if db.bind is not None and db.bind.dialect.name == "sqlite":
        elapsed = (func.julianday(ended_at) - func.julianday(started_at)) * 86400.0
    else:
        elapsed = func.extract("epoch", ended_at - started_at)
    return case(
        (and_(started_at.is_not(None), ended_at >= started_at), elapsed),
        else_=None,
    )


def agent_performance_analytics(
    db: Session,
    *,
    query: InboxAgentPerformanceQuery,
) -> InboxAgentPerformanceAnalyticsPage:
    """Aggregate bounded agent metrics in SQL and return one paged projection.

    Assignment totals use assignment effective time. Resolution totals and
    durations use append-only status-transition evidence, credited only to the
    resolving agent's matching assignment at that instant. First-response time
    uses the first recorded human outbound after the first inbound message for
    conversations first received inside the requested interval.
    """

    start_at = _as_utc(query.start_at)
    end_at = _as_utc(query.end_at)
    if start_at is None or end_at is None:
        raise InboxAgentPerformanceQueryError("Date bounds must be timezone-aware.")

    display_name = func.coalesce(
        func.nullif(func.trim(SystemUser.display_name), ""),
        func.nullif(func.trim(SystemUser.first_name + " " + SystemUser.last_name), ""),
        SystemUser.email,
    )
    member_statement = (
        select(
            ServiceTeamMember.team_id.label("service_team_id"),
            ServiceTeam.name.label("service_team_name"),
            SystemUser.id.label("person_id"),
            display_name.label("agent_name"),
        )
        .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
        .join(SystemUser, SystemUser.person_party_id == ServiceTeamMember.person_id)
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
            SystemUser.is_active.is_(True),
        )
    )
    if query.person_id is not None:
        member_statement = member_statement.where(SystemUser.id == query.person_id)
    normalized_search = (query.search or "").strip()
    if normalized_search:
        search_term = f"%{normalized_search}%"
        member_statement = member_statement.where(
            or_(
                SystemUser.display_name.ilike(search_term),
                SystemUser.first_name.ilike(search_term),
                SystemUser.last_name.ilike(search_term),
                SystemUser.email.ilike(search_term),
            )
        )
    members = member_statement.cte("agent_performance_members")

    assigned = (
        select(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
            func.count(
                func.distinct(InboxConversationAssignment.conversation_id)
            ).label("assigned_count"),
        )
        .where(
            InboxConversationAssignment.assigned_at >= start_at,
            InboxConversationAssignment.assigned_at < end_at,
        )
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
        .cte("agent_period_assignments")
    )
    active = (
        select(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
            func.count(InboxConversationAssignment.id).label("active_count"),
        )
        .where(InboxConversationAssignment.is_active.is_(True))
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
        .cte("agent_active_assignments")
    )

    resolution_duration = _analytics_duration_seconds(
        db,
        started_at=InboxConversation.first_message_at,
        ended_at=InboxStatusTransitionEvent.occurred_at,
    )
    resolution_facts = (
        select(
            InboxStatusTransitionEvent.id.label("event_id"),
            InboxConversationAssignment.service_team_id,
            InboxStatusTransitionEvent.actor_person_id.label("person_id"),
            resolution_duration.label("duration_seconds"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxStatusTransitionEvent.conversation_id,
        )
        .join(
            InboxConversationAssignment,
            and_(
                InboxConversationAssignment.conversation_id
                == InboxStatusTransitionEvent.conversation_id,
                InboxConversationAssignment.person_id
                == InboxStatusTransitionEvent.actor_person_id,
                InboxConversationAssignment.assigned_at
                <= InboxStatusTransitionEvent.occurred_at,
                or_(
                    InboxConversationAssignment.ended_at.is_(None),
                    InboxConversationAssignment.ended_at
                    >= InboxStatusTransitionEvent.occurred_at,
                ),
            ),
        )
        .where(
            InboxStatusTransitionEvent.status == InboxConversationStatus.resolved.value,
            InboxStatusTransitionEvent.actor_person_id.is_not(None),
            InboxStatusTransitionEvent.occurred_at >= start_at,
            InboxStatusTransitionEvent.occurred_at < end_at,
        )
        .distinct()
        .cte("agent_resolution_facts")
    )
    resolution = (
        select(
            resolution_facts.c.service_team_id,
            resolution_facts.c.person_id,
            func.count(func.distinct(resolution_facts.c.event_id)).label(
                "resolved_count"
            ),
            func.count(resolution_facts.c.duration_seconds).label(
                "resolution_timed_count"
            ),
            func.coalesce(func.sum(resolution_facts.c.duration_seconds), 0.0).label(
                "resolution_seconds_sum"
            ),
        )
        .group_by(
            resolution_facts.c.service_team_id,
            resolution_facts.c.person_id,
        )
        .cte("agent_resolutions")
    )

    message_time = func.coalesce(
        InboxMessage.received_at,
        InboxMessage.sent_at,
        InboxMessage.created_at,
    )
    first_inbound_time = func.min(message_time)
    first_inbound = (
        select(
            InboxMessage.conversation_id,
            first_inbound_time.label("first_inbound_at"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxMessage.conversation_id,
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.inbound.value,
            InboxConversation.first_message_at >= start_at,
            InboxConversation.first_message_at < end_at,
        )
        .group_by(InboxMessage.conversation_id)
        .cte("agent_first_inbound")
    )
    sender_value = InboxMessage.metadata_["sent_by_person_id"].as_string()
    outbound_ranked = (
        select(
            InboxMessage.conversation_id,
            sender_value.label("sender_person_id"),
            message_time.label("response_at"),
            first_inbound.c.first_inbound_at,
            func.row_number()
            .over(
                partition_by=InboxMessage.conversation_id,
                order_by=(message_time.asc(), InboxMessage.id.asc()),
            )
            .label("response_rank"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.outbound.value,
            sender_value.is_not(None),
            message_time >= first_inbound.c.first_inbound_at,
            message_time < end_at,
        )
        .cte("agent_human_outbounds")
    )
    first_response = (
        select(
            outbound_ranked.c.conversation_id,
            outbound_ranked.c.sender_person_id,
            outbound_ranked.c.response_at,
            outbound_ranked.c.first_inbound_at,
        )
        .where(outbound_ranked.c.response_rank == 1)
        .cte("agent_first_response")
    )
    response_duration = _analytics_duration_seconds(
        db,
        started_at=first_response.c.first_inbound_at,
        ended_at=first_response.c.response_at,
    )
    assignment_person_text = func.replace(
        cast(InboxConversationAssignment.person_id, String), "-", ""
    )
    response_person_text = func.replace(first_response.c.sender_person_id, "-", "")
    response_facts = (
        select(
            first_response.c.conversation_id,
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
            response_duration.label("duration_seconds"),
        )
        .join(
            InboxConversationAssignment,
            and_(
                InboxConversationAssignment.conversation_id
                == first_response.c.conversation_id,
                assignment_person_text == response_person_text,
                InboxConversationAssignment.assigned_at <= first_response.c.response_at,
                or_(
                    InboxConversationAssignment.ended_at.is_(None),
                    InboxConversationAssignment.ended_at
                    >= first_response.c.response_at,
                ),
            ),
        )
        .distinct()
        .cte("agent_first_response_facts")
    )
    responses = (
        select(
            response_facts.c.service_team_id,
            response_facts.c.person_id,
            func.count(response_facts.c.duration_seconds).label("response_count"),
            func.coalesce(func.sum(response_facts.c.duration_seconds), 0.0).label(
                "response_seconds_sum"
            ),
        )
        .group_by(
            response_facts.c.service_team_id,
            response_facts.c.person_id,
        )
        .cte("agent_first_responses")
    )

    assigned_count = func.coalesce(assigned.c.assigned_count, 0)
    resolved_count = func.coalesce(resolution.c.resolved_count, 0)
    active_count = func.coalesce(active.c.active_count, 0)
    resolution_timed_count = func.coalesce(resolution.c.resolution_timed_count, 0)
    resolution_seconds_sum = func.coalesce(resolution.c.resolution_seconds_sum, 0.0)
    response_count = func.coalesce(responses.c.response_count, 0)
    response_seconds_sum = func.coalesce(responses.c.response_seconds_sum, 0.0)
    performance = (
        select(
            members.c.person_id,
            members.c.agent_name,
            members.c.service_team_id,
            members.c.service_team_name,
            assigned_count.label("assigned_count"),
            resolved_count.label("resolved_count"),
            active_count.label("active_count"),
            resolution_timed_count.label("resolution_timed_count"),
            resolution_seconds_sum.label("resolution_seconds_sum"),
            response_count.label("response_count"),
            response_seconds_sum.label("response_seconds_sum"),
            case(
                (
                    resolution_timed_count > 0,
                    resolution_seconds_sum / resolution_timed_count,
                ),
                else_=None,
            ).label("average_resolution_seconds"),
            case(
                (
                    response_count > 0,
                    response_seconds_sum / response_count,
                ),
                else_=None,
            ).label("average_first_response_seconds"),
        )
        .outerjoin(
            assigned,
            and_(
                assigned.c.service_team_id == members.c.service_team_id,
                assigned.c.person_id == members.c.person_id,
            ),
        )
        .outerjoin(
            active,
            and_(
                active.c.service_team_id == members.c.service_team_id,
                active.c.person_id == members.c.person_id,
            ),
        )
        .outerjoin(
            resolution,
            and_(
                resolution.c.service_team_id == members.c.service_team_id,
                resolution.c.person_id == members.c.person_id,
            ),
        )
        .outerjoin(
            responses,
            and_(
                responses.c.service_team_id == members.c.service_team_id,
                responses.c.person_id == members.c.person_id,
            ),
        )
        .cte("agent_performance")
    )

    total = int(db.scalar(select(func.count()).select_from(members)) or 0)
    if total == 0:
        return InboxAgentPerformanceAnalyticsPage(
            rows=(),
            summary=InboxAgentPerformanceSummary(
                agent_count=0,
                assigned_conversation_count=0,
                resolved_conversation_count=0,
                active_assignment_count=0,
                average_resolution_seconds=None,
                average_first_response_seconds=None,
            ),
            total=0,
            page=1,
            per_page=query.per_page or 1,
            generated_at=datetime.now(UTC),
        )

    summary_values = (
        select(
            func.count(func.distinct(performance.c.person_id)).label(
                "summary_agent_count"
            ),
            func.coalesce(func.sum(performance.c.assigned_count), 0).label(
                "summary_assigned_count"
            ),
            func.coalesce(func.sum(performance.c.resolved_count), 0).label(
                "summary_resolved_count"
            ),
            func.coalesce(func.sum(performance.c.active_count), 0).label(
                "summary_active_count"
            ),
            func.coalesce(func.sum(performance.c.resolution_timed_count), 0).label(
                "summary_resolution_timed_count"
            ),
            func.coalesce(func.sum(performance.c.resolution_seconds_sum), 0.0).label(
                "summary_resolution_seconds_sum"
            ),
            func.coalesce(func.sum(performance.c.response_count), 0).label(
                "summary_response_count"
            ),
            func.coalesce(func.sum(performance.c.response_seconds_sum), 0.0).label(
                "summary_response_seconds_sum"
            ),
        )
        .select_from(performance)
        .cte("agent_performance_summary")
    )
    row_statement = (
        select(performance, summary_values)
        .select_from(performance.join(summary_values, true()))
        .order_by(
            performance.c.agent_name.asc(),
            performance.c.service_team_name.asc(),
            performance.c.person_id.asc(),
        )
    )
    effective_page = query.page
    if query.per_page is not None:
        last_page = max((total + query.per_page - 1) // query.per_page, 1)
        effective_page = min(query.page, last_page)
        row_statement = row_statement.offset(
            (effective_page - 1) * query.per_page
        ).limit(query.per_page)
    raw_rows = db.execute(row_statement).mappings().all()
    summary_row = raw_rows[0]

    resolution_timed_total = int(summary_row["summary_resolution_timed_count"] or 0)
    response_total = int(summary_row["summary_response_count"] or 0)
    summary = InboxAgentPerformanceSummary(
        agent_count=int(summary_row["summary_agent_count"] or 0),
        assigned_conversation_count=int(summary_row["summary_assigned_count"] or 0),
        resolved_conversation_count=int(summary_row["summary_resolved_count"] or 0),
        active_assignment_count=int(summary_row["summary_active_count"] or 0),
        average_resolution_seconds=(
            round(
                float(summary_row["summary_resolution_seconds_sum"])
                / resolution_timed_total,
                3,
            )
            if resolution_timed_total
            else None
        ),
        average_first_response_seconds=(
            round(
                float(summary_row["summary_response_seconds_sum"]) / response_total,
                3,
            )
            if response_total
            else None
        ),
    )
    rows = tuple(
        InboxAgentPerformanceAnalyticsRow(
            person_id=row["person_id"],
            agent_name=str(row["agent_name"]),
            service_team_id=row["service_team_id"],
            service_team_name=str(row["service_team_name"]),
            assigned_conversation_count=int(row["assigned_count"] or 0),
            resolved_conversation_count=int(row["resolved_count"] or 0),
            active_assignment_count=int(row["active_count"] or 0),
            average_resolution_seconds=(
                round(float(row["average_resolution_seconds"]), 3)
                if row["average_resolution_seconds"] is not None
                else None
            ),
            average_first_response_seconds=(
                round(float(row["average_first_response_seconds"]), 3)
                if row["average_first_response_seconds"] is not None
                else None
            ),
        )
        for row in raw_rows
    )
    per_page = query.per_page or max(total, 1)
    return InboxAgentPerformanceAnalyticsPage(
        rows=rows,
        summary=summary,
        total=total,
        page=effective_page,
        per_page=per_page,
        generated_at=datetime.now(UTC),
    )


def active_service_team_options(db: Session) -> list[ServiceTeam]:
    return (
        db.query(ServiceTeam)
        .filter(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name.asc())
        .all()
    )


def escalation_candidates(
    db: Session,
    *,
    response_sla_seconds: int | None = None,
    queue_sla_seconds: int | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[InboxEscalationCandidate]:
    now_utc = _as_utc(now) or datetime.now(UTC)
    team_query = db.query(ServiceTeam).order_by(ServiceTeam.name.asc())
    if not include_inactive:
        team_query = team_query.filter(ServiceTeam.is_active.is_(True))

    teams = team_query.all()
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for team in teams),
    )
    candidates: list[InboxEscalationCandidate] = []
    for team in teams:
        team_response_sla = response_sla_seconds_for_team(
            team,
            fallback=response_sla_seconds,
        )
        team_queue_sla = queue_sla_seconds_for_team(
            team,
            fallback=queue_sla_seconds,
        )
        conversation_ids = _conversation_ids_for_team(db, team.id)
        if not conversation_ids:
            continue

        conversations = (
            db.query(InboxConversation)
            .filter(InboxConversation.id.in_(conversation_ids))
            .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
            .filter(InboxConversation.is_active.is_(True))
            .all()
        )
        if not conversations:
            continue

        messages_by_conversation = _messages_by_conversation(db, conversation_ids)
        active_assignments = {
            row.conversation_id: row
            for row in db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id.in_(conversation_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        available_agent_count = len(
            team_inbox_assignment.list_available_team_agents(db, team.id)
        )

        for conversation in conversations:
            messages = messages_by_conversation.get(conversation.id, [])
            first_inbound = _first_inbound_message(messages)
            pending_response_seconds = None
            reasons: list[str] = []
            if first_inbound is not None:
                inbound_at = _message_time(first_inbound)
                if not _has_outbound_after(messages, inbound_at):
                    pending_response_seconds = _seconds_between(inbound_at, now_utc)
                    if (
                        team_response_sla is not None
                        and pending_response_seconds is not None
                        and pending_response_seconds > team_response_sla
                    ):
                        reasons.append("response_sla_breached")

            assignment = active_assignments.get(conversation.id)
            queue_wait_seconds = _seconds_between(
                conversation.first_message_at,
                assignment.assigned_at if assignment is not None else now_utc,
            )
            if (
                assignment is None
                and team_queue_sla is not None
                and queue_wait_seconds is not None
                and queue_wait_seconds > team_queue_sla
            ):
                reasons.append("unassigned_queue_breached")
            if assignment is None and available_agent_count == 0:
                reasons.append("no_available_agent")

            if not reasons:
                continue

            candidates.append(
                InboxEscalationCandidate(
                    conversation_id=str(conversation.id),
                    service_team_id=str(team.id),
                    service_team_name=team.name,
                    service_team_capabilities=tuple(
                        capability.value for capability in capabilities_by_team[team.id]
                    ),
                    subject=conversation.subject,
                    contact_address=conversation.contact_address,
                    status=conversation.status,
                    reasons=tuple(reasons),
                    response_sla_seconds=team_response_sla,
                    queue_sla_seconds=team_queue_sla,
                    pending_response_seconds=pending_response_seconds,
                    queue_wait_seconds=queue_wait_seconds,
                    assigned_person_id=(
                        str(assignment.person_id) if assignment is not None else None
                    ),
                    available_agent_count=available_agent_count,
                )
            )

    candidates.sort(
        key=lambda item: (
            "response_sla_breached" not in item.reasons,
            -(item.pending_response_seconds or 0),
            -(item.queue_wait_seconds or 0),
            item.service_team_name,
        )
    )
    return candidates
