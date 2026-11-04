"""Read-only, scope-aware evidence projections for Manager AI.

This module belongs to the Team Inbox read side.  It determines factual
conversation cohorts and bounded evidence; Manager AI only interprets the
result and never reads Inbox ORM rows itself.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
    InboxRoutingEvent,
    InboxStatusTransitionEvent,
)
from app.services import team_inbox_contact_context
from app.services.workqueue.scope import WorkqueueScope


class ManagerAnalysisMode(StrEnum):
    conversation = "conversation"
    recent_queue = "recent_queue"
    period = "period"


class ManagerAnalysisPeriod(StrEnum):
    today = "today"
    yesterday = "yesterday"
    last_7_days = "last_7_days"
    last_30_days = "last_30_days"
    custom = "custom"


_MAX_EVIDENCE_CONVERSATIONS = 25
_MAX_MESSAGES_PER_EVIDENCE_CONVERSATION = 12
_MAX_RECENT_CONVERSATIONS = 50
_MAX_CUSTOMER_HISTORY_CONVERSATIONS = 13
_MAX_CUSTOMER_HISTORY_MESSAGES = 48
_MAX_CURRENT_CONVERSATION_MESSAGES = _MAX_CUSTOMER_HISTORY_MESSAGES // 2
_MAX_PRIOR_CONVERSATION_MESSAGES = (
    _MAX_CUSTOMER_HISTORY_MESSAGES - _MAX_CURRENT_CONVERSATION_MESSAGES
) // (_MAX_CUSTOMER_HISTORY_CONVERSATIONS - 1)
_MAX_CUSTOMER_HISTORY_BRIEF_MESSAGES = 4
_MAX_CUSTOMER_HISTORY_SUBJECT_CHARS = 160
_MAX_CUSTOMER_HISTORY_BODY_CHARS = 240


@dataclass(frozen=True, slots=True)
class ManagerAnalysisRequest:
    scope: WorkqueueScope
    mode: ManagerAnalysisMode
    conversation_id: UUID | None = None
    period: ManagerAnalysisPeriod = ManagerAnalysisPeriod.last_7_days
    custom_start: date | None = None
    custom_end: date | None = None
    channel_type: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ManagerEvidenceMessage:
    direction: str
    body: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ManagerEvidenceConversation:
    id: UUID
    subject: str | None
    channel_type: str
    current_status: str
    activity_at: datetime
    reasons: tuple[str, ...]
    messages: tuple[ManagerEvidenceMessage, ...]


@dataclass(frozen=True, slots=True)
class ManagerPeriodFacts:
    period_start: datetime
    period_end: datetime
    timezone: str
    cohort_definition: str
    total_conversations: int
    current_state_status_counts: tuple[tuple[str, int], ...]
    channel_counts: tuple[tuple[str, int], ...]
    resolved_transition_count: int
    reopened_conversation_ids: tuple[UUID, ...]
    escalated_conversation_ids: tuple[UUID, ...]
    evidence_count: int


@dataclass(frozen=True, slots=True)
class ManagerAnalysisProjection:
    mode: ManagerAnalysisMode
    facts: ManagerPeriodFacts | None
    selected_conversation: ManagerEvidenceConversation | None
    recent_conversations: tuple[ManagerEvidenceConversation, ...]
    evidence_conversations: tuple[ManagerEvidenceConversation, ...]


@dataclass(frozen=True, slots=True)
class CustomerHistorySummaryRequest:
    conversation_id: UUID
    permitted: bool


@dataclass(frozen=True, slots=True)
class CustomerHistorySourceMessage:
    id: UUID
    conversation_id: UUID
    direction: str
    subject: str | None
    body: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerHistorySummaryProjection:
    conversation_id: UUID
    availability: team_inbox_contact_context.ContextAvailability
    history_scope: team_inbox_contact_context.ConversationHistoryScope
    conversation_ids: tuple[UUID, ...]
    total_conversation_count: int
    messages: tuple[CustomerHistorySourceMessage, ...]
    inspected_message_count: int
    evidence_is_truncated: bool
    source_watermark: str | None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerHistoryBriefMessage:
    direction: str
    body: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerHistoryBrief:
    conversation_id: UUID
    availability: team_inbox_contact_context.ContextAvailability
    previous_conversation_count: int
    customer_message_count: int
    agent_message_count: int
    latest_activity_at: datetime | None
    recent_messages: tuple[CustomerHistoryBriefMessage, ...]
    evidence_conversation_count: int
    evidence_message_count: int
    inspected_message_count: int
    evidence_is_truncated: bool
    source_watermark: str | None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _CustomerHistoryEvidenceBatch:
    messages: tuple[CustomerHistorySourceMessage, ...]
    inspected_count: int
    truncated: bool


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _customer_history_message_evidence(
    db: Session,
    *,
    conversation_id: UUID,
    limit: int,
) -> _CustomerHistoryEvidenceBatch:
    """Read one index-backed, work- and byte-bounded stored-message window."""

    occurred = func.coalesce(
        InboxMessage.received_at,
        InboxMessage.sent_at,
        InboxMessage.created_at,
    )
    rows = db.execute(
        select(
            InboxMessage.id,
            InboxMessage.conversation_id,
            InboxMessage.direction,
            func.substr(InboxMessage.subject, 1, _MAX_CUSTOMER_HISTORY_SUBJECT_CHARS),
            func.substr(InboxMessage.body, 1, _MAX_CUSTOMER_HISTORY_BODY_CHARS),
            occurred,
        )
        .where(InboxMessage.conversation_id == conversation_id)
        # ix_inbox_messages_conversation leads on (conversation_id, created_at).
        # Filtering private/empty rows after this limited read prevents a large
        # run of notes from turning a drawer open into an unbounded scan.
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .limit(limit + 1)
    ).all()
    selected = rows[:limit]
    messages = tuple(
        CustomerHistorySourceMessage(
            id=identifier,
            conversation_id=row_conversation_id,
            direction=direction,
            subject=subject,
            body=body or "",
            occurred_at=_utc(occurred_at),
        )
        for (
            identifier,
            row_conversation_id,
            direction,
            subject,
            body,
            occurred_at,
        ) in reversed(selected)
        if direction in {"inbound", "outbound"} and (body or "").strip()
    )
    return _CustomerHistoryEvidenceBatch(
        messages=messages,
        inspected_count=len(selected),
        truncated=len(rows) > limit,
    )


def build_customer_history_summary_projection(
    db: Session,
    request: CustomerHistorySummaryRequest,
) -> CustomerHistorySummaryProjection | None:
    """Return public conversation evidence from one reviewed identity cohort."""

    history = team_inbox_contact_context.eligible_conversation_history(
        db,
        team_inbox_contact_context.EligibleConversationHistoryQuery(
            conversation_id=request.conversation_id,
            permitted=request.permitted,
            evidence_limit=_MAX_CUSTOMER_HISTORY_CONVERSATIONS,
        ),
    )
    if history is None:
        return None
    if (
        history.availability
        is not team_inbox_contact_context.ContextAvailability.available
    ):
        return CustomerHistorySummaryProjection(
            conversation_id=request.conversation_id,
            availability=history.availability,
            history_scope=history.scope,
            conversation_ids=(),
            total_conversation_count=0,
            messages=(),
            inspected_message_count=0,
            evidence_is_truncated=False,
            source_watermark=None,
            message=history.message,
        )
    current_batch = _customer_history_message_evidence(
        db,
        conversation_id=request.conversation_id,
        limit=_MAX_CURRENT_CONVERSATION_MESSAGES,
    )
    prior_batches = tuple(
        _customer_history_message_evidence(
            db,
            conversation_id=conversation_id,
            limit=_MAX_PRIOR_CONVERSATION_MESSAGES,
        )
        for conversation_id in history.conversation_ids
        if conversation_id != request.conversation_id
    )
    messages = tuple(
        sorted(
            (
                *current_batch.messages,
                *(message for batch in prior_batches for message in batch.messages),
            ),
            key=lambda message: (message.occurred_at, str(message.id)),
        )
    )
    inspected_message_count = current_batch.inspected_count + sum(
        batch.inspected_count for batch in prior_batches
    )
    messages_are_truncated = current_batch.truncated or any(
        batch.truncated for batch in prior_batches
    )
    total_conversation_count = history.total_conversation_count or len(
        history.conversation_ids
    )
    evidence_is_truncated = messages_are_truncated or total_conversation_count > len(
        history.conversation_ids
    )
    digest = hashlib.sha256()
    digest.update(f"total-conversations:{total_conversation_count}\n".encode())
    digest.update(f"selected-conversations:{len(history.conversation_ids)}\n".encode())
    digest.update(f"inspected-messages:{inspected_message_count}\n".encode())
    digest.update(f"evidence-truncated:{int(evidence_is_truncated)}\n".encode())
    for conversation_id in history.conversation_ids:
        digest.update(f"conversation:{conversation_id}\n".encode())
    for message in messages:
        digest.update(
            (
                f"message:{message.id}:{message.conversation_id}:"
                f"{message.direction}:{message.occurred_at.isoformat()}\n"
            ).encode()
        )
        digest.update((message.subject or "").encode())
        digest.update(b"\0")
        digest.update(message.body.encode())
        digest.update(b"\n")
    return CustomerHistorySummaryProjection(
        conversation_id=request.conversation_id,
        availability=history.availability,
        history_scope=history.scope,
        conversation_ids=history.conversation_ids,
        total_conversation_count=total_conversation_count,
        messages=messages,
        inspected_message_count=inspected_message_count,
        evidence_is_truncated=evidence_is_truncated,
        source_watermark=digest.hexdigest(),
    )


def _brief_body(value: str, *, limit: int = 240) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else f"{clean[: limit - 1].rstrip()}…"


def build_customer_history_brief(
    db: Session,
    request: CustomerHistorySummaryRequest,
) -> CustomerHistoryBrief | None:
    """Return a bounded factual brief from public customer conversation evidence."""

    source = build_customer_history_summary_projection(db, request)
    if source is None:
        return None
    if (
        source.availability
        is not team_inbox_contact_context.ContextAvailability.available
    ):
        return CustomerHistoryBrief(
            conversation_id=request.conversation_id,
            availability=source.availability,
            previous_conversation_count=0,
            customer_message_count=0,
            agent_message_count=0,
            latest_activity_at=None,
            recent_messages=(),
            evidence_conversation_count=0,
            evidence_message_count=0,
            inspected_message_count=0,
            evidence_is_truncated=False,
            source_watermark=source.source_watermark,
            message=source.message,
        )

    public_messages = tuple(
        message for message in source.messages if message.body.strip()
    )
    if not public_messages:
        if source.evidence_is_truncated:
            return CustomerHistoryBrief(
                conversation_id=request.conversation_id,
                availability=team_inbox_contact_context.ContextAvailability.unavailable,
                previous_conversation_count=max(source.total_conversation_count - 1, 0),
                customer_message_count=0,
                agent_message_count=0,
                latest_activity_at=None,
                recent_messages=(),
                evidence_conversation_count=0,
                evidence_message_count=0,
                # Do not let the public-evidence projection disclose how many
                # private/blank records occupied the bounded stored window.
                inspected_message_count=0,
                evidence_is_truncated=True,
                source_watermark=source.source_watermark,
                message=(
                    "No public messages were found in the bounded recent window. "
                    "Older records were not scanned."
                ),
            )
        return CustomerHistoryBrief(
            conversation_id=request.conversation_id,
            availability=team_inbox_contact_context.ContextAvailability.empty,
            previous_conversation_count=max(source.total_conversation_count - 1, 0),
            customer_message_count=0,
            agent_message_count=0,
            latest_activity_at=None,
            recent_messages=(),
            evidence_conversation_count=0,
            evidence_message_count=0,
            inspected_message_count=source.inspected_message_count,
            evidence_is_truncated=source.evidence_is_truncated,
            source_watermark=source.source_watermark,
            message="No public customer conversation messages yet.",
        )

    return CustomerHistoryBrief(
        conversation_id=request.conversation_id,
        availability=team_inbox_contact_context.ContextAvailability.available,
        previous_conversation_count=max(source.total_conversation_count - 1, 0),
        customer_message_count=sum(
            message.direction == "inbound" for message in public_messages
        ),
        agent_message_count=sum(
            message.direction == "outbound" for message in public_messages
        ),
        latest_activity_at=public_messages[-1].occurred_at,
        recent_messages=tuple(
            CustomerHistoryBriefMessage(
                direction=message.direction,
                body=_brief_body(message.body),
                occurred_at=message.occurred_at,
            )
            for message in public_messages[-_MAX_CUSTOMER_HISTORY_BRIEF_MESSAGES:]
        ),
        evidence_conversation_count=len(
            {message.conversation_id for message in public_messages}
        ),
        evidence_message_count=len(public_messages),
        inspected_message_count=source.inspected_message_count,
        evidence_is_truncated=source.evidence_is_truncated,
        source_watermark=source.source_watermark,
    )


def resolve_period(
    request: ManagerAnalysisRequest, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Return a half-open UTC period using the application's stored UTC time."""

    current = _utc(now or datetime.now(UTC))
    today = current.date()
    if request.period is ManagerAnalysisPeriod.today:
        start, end = today, today + timedelta(days=1)
    elif request.period is ManagerAnalysisPeriod.yesterday:
        start, end = today - timedelta(days=1), today
    elif request.period is ManagerAnalysisPeriod.last_7_days:
        start, end = today - timedelta(days=6), today + timedelta(days=1)
    elif request.period is ManagerAnalysisPeriod.last_30_days:
        start, end = today - timedelta(days=29), today + timedelta(days=1)
    else:
        if request.custom_start is None or request.custom_end is None:
            raise ValueError("Custom period needs both a start and end date.")
        if request.custom_end < request.custom_start:
            raise ValueError(
                "Custom period end date must not be before its start date."
            )
        start, end = request.custom_start, request.custom_end + timedelta(days=1)
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end, time.min, tzinfo=UTC),
    )


def _visible_conversations_query(scope: WorkqueueScope):
    assignment = InboxConversationAssignment
    query = select(InboxConversation).where(InboxConversation.is_active.is_(True))
    if not scope.is_org_wide:
        team_ids = scope.team_ids_for_query()
        person_visibility = (
            exists()
            .where(assignment.conversation_id == InboxConversation.id)
            .where(assignment.is_active.is_(True))
            .where(assignment.person_id == scope.person_id)
        )
        if team_ids:
            query = query.where(
                or_(
                    person_visibility,
                    InboxConversation.primary_service_team_id.in_(team_ids),
                )
            )
        else:
            query = query.where(person_visibility)
    elif scope.service_team_filter is not None:
        query = query.where(
            InboxConversation.primary_service_team_id == scope.service_team_filter
        )
    return query


def _activity_cohort_ids(
    db: Session, request: ManagerAnalysisRequest, start: datetime, end: datetime
) -> tuple[UUID, ...]:
    visible = _visible_conversations_query(request.scope).subquery()
    message_time = InboxMessage.received_at
    message_ids = (
        select(InboxMessage.conversation_id)
        .join(visible, visible.c.id == InboxMessage.conversation_id)
        .where(
            # Provider occurrence time is preferred; stored time closes the explicit
            # unknown-time gap without changing the message's canonical chronology.
            func.coalesce(message_time, InboxMessage.sent_at, InboxMessage.created_at)
            >= start,
            func.coalesce(message_time, InboxMessage.sent_at, InboxMessage.created_at)
            < end,
        )
    )
    status_ids = (
        select(InboxStatusTransitionEvent.conversation_id)
        .join(visible, visible.c.id == InboxStatusTransitionEvent.conversation_id)
        .where(
            InboxStatusTransitionEvent.occurred_at >= start,
            InboxStatusTransitionEvent.occurred_at < end,
        )
    )
    routing_ids = (
        select(InboxRoutingEvent.conversation_id)
        .join(visible, visible.c.id == InboxRoutingEvent.conversation_id)
        .where(
            InboxRoutingEvent.occurred_at >= start, InboxRoutingEvent.occurred_at < end
        )
    )
    return tuple(db.execute(message_ids.union(status_ids, routing_ids)).scalars().all())


def _messages_for_evidence(
    db: Session, ids: tuple[UUID, ...], start: datetime | None, end: datetime | None
) -> dict[UUID, tuple[ManagerEvidenceMessage, ...]]:
    if not ids:
        return {}
    occurred = func.coalesce(
        InboxMessage.received_at, InboxMessage.sent_at, InboxMessage.created_at
    )
    query = select(InboxMessage).where(InboxMessage.conversation_id.in_(ids))
    if start is not None and end is not None:
        query = query.where(occurred >= start, occurred < end)
    rows = (
        db.execute(query.order_by(occurred.desc(), InboxMessage.id.desc()))
        .scalars()
        .all()
    )
    grouped: dict[UUID, list[ManagerEvidenceMessage]] = defaultdict(list)
    for row in rows:
        if len(grouped[row.conversation_id]) >= _MAX_MESSAGES_PER_EVIDENCE_CONVERSATION:
            continue
        grouped[row.conversation_id].append(
            ManagerEvidenceMessage(
                row.direction,
                (row.body or "")[:2000],
                _utc(row.received_at or row.sent_at or row.created_at),
            )
        )
    return {key: tuple(reversed(value)) for key, value in grouped.items()}


def _evidence_rows(
    db: Session,
    ids: tuple[UUID, ...],
    start: datetime | None,
    end: datetime | None,
    *,
    limit: int,
) -> tuple[ManagerEvidenceConversation, ...]:
    if not ids:
        return ()
    status_query = select(InboxStatusTransitionEvent).where(
        InboxStatusTransitionEvent.conversation_id.in_(ids)
    )
    routing_query = select(InboxRoutingEvent).where(
        InboxRoutingEvent.conversation_id.in_(ids)
    )
    if start is not None and end is not None:
        status_query = status_query.where(
            InboxStatusTransitionEvent.occurred_at >= start,
            InboxStatusTransitionEvent.occurred_at < end,
        )
        routing_query = routing_query.where(
            InboxRoutingEvent.occurred_at >= start,
            InboxRoutingEvent.occurred_at < end,
        )
    status_rows = db.execute(status_query).scalars().all()
    routing_rows = db.execute(routing_query).scalars().all()
    reasons: dict[UUID, set[str]] = defaultdict(set)
    latest: dict[UUID, datetime] = {}
    for status_event in status_rows:
        latest[status_event.conversation_id] = max(
            latest.get(status_event.conversation_id, status_event.occurred_at),
            status_event.occurred_at,
        )
        if (
            status_event.previous_status == "resolved"
            and status_event.status != "resolved"
        ):
            reasons[status_event.conversation_id].add("reopened")
        if status_event.status == "resolved":
            reasons[status_event.conversation_id].add("resolved_in_period")
    for routing_event in routing_rows:
        latest[routing_event.conversation_id] = max(
            latest.get(routing_event.conversation_id, routing_event.occurred_at),
            routing_event.occurred_at,
        )
        if routing_event.event_type.value == "escalated":
            reasons[routing_event.conversation_id].add("escalated")
    conversations = {
        row.id: row
        for row in db.execute(
            select(InboxConversation).where(InboxConversation.id.in_(ids))
        )
        .scalars()
        .all()
    }
    for identifier, conversation in conversations.items():
        if conversation.status != "resolved":
            reasons[identifier].add("currently_unresolved")
        latest.setdefault(
            identifier, _utc(conversation.last_message_at or conversation.created_at)
        )
    ranked = sorted(
        ids,
        key=lambda identifier: (len(reasons[identifier]), latest[identifier]),
        reverse=True,
    )[:limit]
    messages = _messages_for_evidence(db, tuple(ranked), start, end)
    return tuple(
        ManagerEvidenceConversation(
            identifier,
            conversations[identifier].subject,
            conversations[identifier].channel_type,
            conversations[identifier].status,
            latest[identifier],
            tuple(sorted(reasons[identifier])),
            messages.get(identifier, ()),
        )
        for identifier in ranked
    )


def build_projection(
    db: Session, request: ManagerAnalysisRequest, *, now: datetime | None = None
) -> ManagerAnalysisProjection:
    """Build factual, bounded Manager-AI input without writes or AI calls."""

    if request.mode is ManagerAnalysisMode.conversation:
        if request.conversation_id is None:
            raise ValueError("Select a conversation.")
        allowed = db.execute(
            _visible_conversations_query(request.scope).where(
                InboxConversation.id == request.conversation_id
            )
        ).scalar_one_or_none()
        if allowed is None:
            raise ValueError("Conversation was not found or is unavailable.")
        evidence = _evidence_rows(
            db,
            (allowed.id,),
            None,
            None,
            limit=1,
        )
        return ManagerAnalysisProjection(
            request.mode, None, evidence[0] if evidence else None, (), evidence
        )

    if request.mode is ManagerAnalysisMode.recent_queue:
        rows = (
            db.execute(
                _visible_conversations_query(request.scope)
                .order_by(
                    InboxConversation.last_message_at.desc().nullslast(),
                    InboxConversation.created_at.desc(),
                )
                .limit(_MAX_RECENT_CONVERSATIONS)
            )
            .scalars()
            .all()
        )
        evidence = _evidence_rows(
            db,
            tuple(row.id for row in rows[:20]),
            None,
            None,
            limit=20,
        )
        return ManagerAnalysisProjection(request.mode, None, None, evidence, evidence)

    start, end = resolve_period(request, now=now)
    ids = _activity_cohort_ids(db, request, start, end)
    conversations = (
        db.execute(select(InboxConversation).where(InboxConversation.id.in_(ids)))
        .scalars()
        .all()
        if ids
        else []
    )
    if request.channel_type:
        conversations = [
            row for row in conversations if row.channel_type == request.channel_type
        ]
    if request.status:
        conversations = [row for row in conversations if row.status == request.status]
    ids = tuple(row.id for row in conversations)
    status_rows = (
        db.execute(
            select(InboxStatusTransitionEvent).where(
                InboxStatusTransitionEvent.conversation_id.in_(ids),
                InboxStatusTransitionEvent.occurred_at >= start,
                InboxStatusTransitionEvent.occurred_at < end,
            )
        )
        .scalars()
        .all()
        if ids
        else []
    )
    routing_rows = (
        db.execute(
            select(InboxRoutingEvent).where(
                InboxRoutingEvent.conversation_id.in_(ids),
                InboxRoutingEvent.occurred_at >= start,
                InboxRoutingEvent.occurred_at < end,
            )
        )
        .scalars()
        .all()
        if ids
        else []
    )
    reopened = tuple(
        sorted(
            {
                row.conversation_id
                for row in status_rows
                if row.previous_status == "resolved" and row.status != "resolved"
            },
            key=str,
        )
    )
    escalated = tuple(
        sorted(
            {
                row.conversation_id
                for row in routing_rows
                if row.event_type.value == "escalated"
            },
            key=str,
        )
    )
    evidence = _evidence_rows(db, ids, start, end, limit=_MAX_EVIDENCE_CONVERSATIONS)
    facts = ManagerPeriodFacts(
        start,
        end,
        "UTC",
        "A conversation is in the cohort when it has a canonical Inbox message, status transition, or routing event in the half-open selected UTC period.",
        len(ids),
        tuple(sorted(Counter(row.status for row in conversations).items())),
        tuple(sorted(Counter(row.channel_type for row in conversations).items())),
        sum(1 for row in status_rows if row.status == "resolved"),
        reopened,
        escalated,
        len(evidence),
    )
    return ManagerAnalysisProjection(request.mode, facts, None, (), evidence)
