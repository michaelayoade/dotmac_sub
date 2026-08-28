from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from uuid import UUID

from sqlalchemy import case, distinct, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
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
    service_team_id: str
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    metrics: InboxAgentPerformanceMetrics


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


def _message_time_sql():
    return func.coalesce(
        InboxMessage.received_at, InboxMessage.sent_at, InboxMessage.created_at
    )


def _seconds_sql(db: Session, end, start):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return (func.julianday(end) - func.julianday(start)) * 86400.0
    return func.extract("epoch", end - start)


def _numeric(value: object) -> float | None:
    return float(value) if value is not None else None


def _uuid_value(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _team_performance_by_team(
    db: Session,
    team_ids: tuple[UUID, ...],
    *,
    response_sla_seconds_by_team: dict[UUID, int | None],
    now: datetime | None = None,
) -> dict[UUID, InboxTeamPerformanceMetrics]:
    now_utc = _as_utc(now) or datetime.now(UTC)
    empty = {
        team_id: InboxTeamPerformanceMetrics(
            service_team_id=str(team_id),
            conversation_count=0,
            open_count=0,
            unassigned_open_count=0,
            assigned_open_count=0,
            inbound_message_count=0,
            outbound_message_count=0,
            responded_count=0,
            response_sla_breached_count=0,
            average_first_response_seconds=None,
            average_queue_wait_seconds=None,
        )
        for team_id in team_ids
    }
    if not team_ids:
        return empty

    links = (
        select(
            InboxConversationTeam.service_team_id.label("team_id"),
            InboxConversationTeam.conversation_id.label("conversation_id"),
            InboxConversation.status.label("status"),
            InboxConversation.first_message_at.label("first_message_at"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationTeam.conversation_id,
        )
        .where(InboxConversationTeam.service_team_id.in_(team_ids))
        .where(InboxConversationTeam.is_active.is_(True))
        .subquery()
    )
    conversation_scope = select(
        distinct(links.c.conversation_id).label("conversation_id")
    ).subquery()

    queue_wait = _seconds_sql(
        db,
        InboxConversationAssignment.assigned_at,
        links.c.first_message_at,
    )
    conversation_rows = db.execute(
        select(
            links.c.team_id,
            func.count(links.c.conversation_id).label("conversation_count"),
            func.sum(
                case(
                    (links.c.status != InboxConversationStatus.resolved.value, 1),
                    else_=0,
                )
            ).label("open_count"),
            func.sum(
                case(
                    (
                        (links.c.status != InboxConversationStatus.resolved.value)
                        & (InboxConversationAssignment.id.is_not(None)),
                        1,
                    ),
                    else_=0,
                )
            ).label("assigned_open_count"),
            func.avg(queue_wait).label("average_queue_wait_seconds"),
        )
        .select_from(
            links.outerjoin(
                InboxConversationAssignment,
                (
                    InboxConversationAssignment.conversation_id
                    == links.c.conversation_id
                )
                & (InboxConversationAssignment.is_active.is_(True)),
            )
        )
        .group_by(links.c.team_id)
    ).all()
    conversation_values: dict[UUID, dict[str, float | int | None]] = {}
    for row in conversation_rows:
        conversation_count = int(row.conversation_count or 0)
        open_count = int(row.open_count or 0)
        assigned_open_count = int(row.assigned_open_count or 0)
        conversation_values[_uuid_value(row.team_id)] = {
            "conversation_count": conversation_count,
            "open_count": open_count,
            "assigned_open_count": assigned_open_count,
            "unassigned_open_count": open_count - assigned_open_count,
            "average_queue_wait_seconds": _numeric(row.average_queue_wait_seconds),
        }

    message_rows = db.execute(
        select(
            links.c.team_id,
            func.sum(
                case(
                    (InboxMessage.direction == InboxMessageDirection.inbound.value, 1),
                    else_=0,
                )
            ).label("inbound_count"),
            func.sum(
                case(
                    (InboxMessage.direction == InboxMessageDirection.outbound.value, 1),
                    else_=0,
                )
            ).label("outbound_count"),
        )
        .select_from(
            links.join(
                InboxMessage,
                InboxMessage.conversation_id == links.c.conversation_id,
            )
        )
        .group_by(links.c.team_id)
    ).all()
    message_values = {
        _uuid_value(row.team_id): {
            "inbound_message_count": int(row.inbound_count or 0),
            "outbound_message_count": int(row.outbound_count or 0),
        }
        for row in message_rows
    }

    message_time = _message_time_sql()
    first_inbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(message_time).label("first_inbound_at"),
        )
        .join(
            conversation_scope,
            conversation_scope.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )
    outbound_time = _message_time_sql()
    first_outbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(outbound_time).label("first_outbound_at"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .where(outbound_time >= first_inbound.c.first_inbound_at)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )
    response_seconds = _seconds_sql(
        db,
        first_outbound.c.first_outbound_at,
        first_inbound.c.first_inbound_at,
    )
    pending_seconds = _seconds_sql(db, literal(now_utc), first_inbound.c.first_inbound_at)
    response_rows = db.execute(
        select(
            links.c.team_id,
            first_outbound.c.first_outbound_at,
            response_seconds.label("response_seconds"),
            pending_seconds.label("pending_seconds"),
        )
        .select_from(
            links.join(
                first_inbound,
                first_inbound.c.conversation_id == links.c.conversation_id,
            ).outerjoin(
                first_outbound,
                first_outbound.c.conversation_id == links.c.conversation_id,
            )
        )
    ).all()

    response_values_by_team: dict[UUID, list[float]] = {
        team_id: [] for team_id in team_ids
    }
    response_breaches_by_team = dict.fromkeys(team_ids, 0)
    for row in response_rows:
        team_id = _uuid_value(row.team_id)
        sla = response_sla_seconds_by_team.get(team_id)
        if row.first_outbound_at is not None:
            value = float(row.response_seconds or 0)
            response_values_by_team[team_id].append(value)
            if sla is not None and value > sla:
                response_breaches_by_team[team_id] += 1
        elif (
            sla is not None
            and row.pending_seconds is not None
            and float(row.pending_seconds) > sla
        ):
            response_breaches_by_team[team_id] += 1

    return {
        team_id: InboxTeamPerformanceMetrics(
            service_team_id=str(team_id),
            conversation_count=int(
                conversation_values.get(team_id, {}).get("conversation_count") or 0
            ),
            open_count=int(conversation_values.get(team_id, {}).get("open_count") or 0),
            unassigned_open_count=int(
                conversation_values.get(team_id, {}).get("unassigned_open_count") or 0
            ),
            assigned_open_count=int(
                conversation_values.get(team_id, {}).get("assigned_open_count") or 0
            ),
            inbound_message_count=int(
                message_values.get(team_id, {}).get("inbound_message_count") or 0
            ),
            outbound_message_count=int(
                message_values.get(team_id, {}).get("outbound_message_count") or 0
            ),
            responded_count=len(response_values_by_team[team_id]),
            response_sla_breached_count=response_breaches_by_team[team_id],
            average_first_response_seconds=_avg(response_values_by_team[team_id]),
            average_queue_wait_seconds=conversation_values.get(team_id, {}).get(
                "average_queue_wait_seconds"
            ),
        )
        for team_id in team_ids
    }


def _assignment_values_by_agent(
    db: Session,
    team_ids: tuple[UUID, ...],
) -> dict[tuple[UUID, UUID], dict[str, float | int | None]]:
    if not team_ids:
        return {}

    queue_wait = _seconds_sql(
        db,
        InboxConversationAssignment.assigned_at,
        InboxConversation.first_message_at,
    )
    resolved_conversation_id = case(
        (
            InboxConversation.status == InboxConversationStatus.resolved.value,
            InboxConversationAssignment.conversation_id,
        ),
        else_=None,
    )
    rows = db.execute(
        select(
            InboxConversationAssignment.service_team_id.label("team_id"),
            InboxConversationAssignment.person_id.label("person_id"),
            func.sum(
                case((InboxConversationAssignment.is_active.is_(True), 1), else_=0)
            ).label("active_assignment_count"),
            func.count(
                distinct(InboxConversationAssignment.conversation_id)
            ).label("handled_conversation_count"),
            func.count(distinct(resolved_conversation_id)).label(
                "resolved_conversation_count"
            ),
            func.avg(queue_wait).label("average_queue_wait_seconds"),
        )
        .select_from(InboxConversationAssignment)
        .outerjoin(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .where(InboxConversationAssignment.service_team_id.in_(team_ids))
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
    ).all()

    return {
        (_uuid_value(row.team_id), _uuid_value(row.person_id)): {
            "active_assignment_count": int(row.active_assignment_count or 0),
            "handled_conversation_count": int(row.handled_conversation_count or 0),
            "resolved_conversation_count": int(row.resolved_conversation_count or 0),
            "average_queue_wait_seconds": _numeric(row.average_queue_wait_seconds),
        }
        for row in rows
    }


def _human_response_seconds_by_agent(
    db: Session,
    team_ids: tuple[UUID, ...],
) -> dict[tuple[UUID, UUID], list[float]]:
    if not team_ids:
        return {}

    links = (
        select(
            InboxConversationTeam.service_team_id.label("team_id"),
            InboxConversationTeam.conversation_id.label("conversation_id"),
        )
        .where(InboxConversationTeam.service_team_id.in_(team_ids))
        .where(InboxConversationTeam.is_active.is_(True))
        .subquery()
    )
    conversation_scope = select(
        distinct(links.c.conversation_id).label("conversation_id")
    ).subquery()

    message_time = _message_time_sql()
    first_inbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(message_time).label("first_inbound_at"),
        )
        .join(
            conversation_scope,
            conversation_scope.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )

    outbound_time = _message_time_sql()
    sent_by_person = InboxMessage.metadata_["sent_by_person_id"].as_string()
    first_human_outbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(outbound_time).label("first_outbound_at"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .where(sent_by_person.is_not(None))
        .where(sent_by_person != "")
        .where(outbound_time >= first_inbound.c.first_inbound_at)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )

    sender_at_first_response = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(InboxMessage.metadata_["sent_by_person_id"].as_string()).label(
                "person_id"
            ),
        )
        .join(
            first_human_outbound,
            (first_human_outbound.c.conversation_id == InboxMessage.conversation_id)
            & (_message_time_sql() == first_human_outbound.c.first_outbound_at),
        )
        .where(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .where(InboxMessage.metadata_["sent_by_person_id"].as_string().is_not(None))
        .where(InboxMessage.metadata_["sent_by_person_id"].as_string() != "")
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )

    response_seconds = _seconds_sql(
        db,
        first_human_outbound.c.first_outbound_at,
        first_inbound.c.first_inbound_at,
    )
    response_rows = db.execute(
        select(
            links.c.team_id,
            sender_at_first_response.c.person_id,
            response_seconds.label("response_seconds"),
        )
        .select_from(
            links.join(
                first_inbound,
                first_inbound.c.conversation_id == links.c.conversation_id,
            )
            .join(
                first_human_outbound,
                first_human_outbound.c.conversation_id == links.c.conversation_id,
            )
            .join(
                sender_at_first_response,
                sender_at_first_response.c.conversation_id == links.c.conversation_id,
            )
        )
    ).all()

    values: dict[tuple[UUID, UUID], list[float]] = {}
    for row in response_rows:
        try:
            key = (_uuid_value(row.team_id), _uuid_value(row.person_id))
        except (TypeError, ValueError):
            continue
        if row.response_seconds is not None:
            values.setdefault(key, []).append(float(row.response_seconds))
    return values


def team_performance_metrics(
    db: Session,
    service_team_id: str | UUID,
    *,
    response_sla_seconds: int | None = None,
    now: datetime | None = None,
) -> InboxTeamPerformanceMetrics:
    team_uuid = UUID(str(service_team_id))
    return _team_performance_by_team(
        db,
        (team_uuid,),
        response_sla_seconds_by_team={team_uuid: response_sla_seconds},
        now=now,
    )[team_uuid]


def agent_performance_metrics(
    db: Session,
    *,
    service_team_id: str | UUID,
    person_id: str | UUID,
) -> InboxAgentPerformanceMetrics:
    team_uuid = UUID(str(service_team_id))
    person_uuid = UUID(str(person_id))
    key = (team_uuid, person_uuid)
    assignment_values = _assignment_values_by_agent(db, (team_uuid,)).get(key, {})
    first_response_values = _human_response_seconds_by_agent(db, (team_uuid,)).get(
        key, []
    )

    return InboxAgentPerformanceMetrics(
        person_id=str(person_uuid),
        service_team_id=str(team_uuid),
        active_assignment_count=int(
            assignment_values.get("active_assignment_count") or 0
        ),
        handled_conversation_count=int(
            assignment_values.get("handled_conversation_count") or 0
        ),
        resolved_conversation_count=int(
            assignment_values.get("resolved_conversation_count") or 0
        ),
        average_first_response_seconds=_avg(first_response_values),
        average_queue_wait_seconds=assignment_values.get(
            "average_queue_wait_seconds"
        ),
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
    response_sla_by_team = {
        team.id: response_sla_seconds_for_team(team, fallback=response_sla_seconds)
        for team in teams
    }
    metrics_by_team = _team_performance_by_team(
        db,
        tuple(team.id for team in teams),
        response_sla_seconds_by_team=response_sla_by_team,
        now=now,
    )
    return [
        InboxTeamPerformanceReportRow(
            service_team_id=str(team.id),
            service_team_name=team.name,
            service_team_capabilities=tuple(
                capability.value for capability in capabilities_by_team[team.id]
            ),
            response_sla_seconds=response_sla_by_team[team.id],
            metrics=metrics_by_team[team.id],
        )
        for team in teams
    ]


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
    assignment_values_by_agent = _assignment_values_by_agent(db, team_ids)
    response_seconds_by_agent = _human_response_seconds_by_agent(db, team_ids)
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        team_ids,
    )

    rows: list[InboxAgentPerformanceReportRow] = []
    for _member, team, user in members:
        key = (team.id, user.id)
        assignment_values = assignment_values_by_agent.get(key, {})
        metrics = InboxAgentPerformanceMetrics(
            person_id=str(user.id),
            service_team_id=str(team.id),
            active_assignment_count=int(
                assignment_values.get("active_assignment_count") or 0
            ),
            handled_conversation_count=int(
                assignment_values.get("handled_conversation_count") or 0
            ),
            resolved_conversation_count=int(
                assignment_values.get("resolved_conversation_count") or 0
            ),
            average_first_response_seconds=_avg(
                response_seconds_by_agent.get(key, [])
            ),
            average_queue_wait_seconds=assignment_values.get(
                "average_queue_wait_seconds"
            ),
        )
        rows.append(
            InboxAgentPerformanceReportRow(
                person_id=str(user.id),
                service_team_id=str(team.id),
                service_team_name=team.name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team.id]
                ),
                metrics=metrics,
            )
        )
    return rows


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
