"""Durable CSAT requests for resolved support interactions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Integer, func, or_
from sqlalchemy.orm import Session

from app.models.csat import CsatRequestStatus, CsatSourceType, SupportCsatRequest
from app.models.service_team import ServiceTeam
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    TeamInboxChannelRoute,
)
from app.services.common import apply_pagination
from app.services.domain_errors import DomainError

logger = logging.getLogger(__name__)

OWNER = "support.csat"
LEGACY_TICKET_CYCLE_KEY = "current_csat_resolution_cycle_key"


class SupportCsatError(DomainError):
    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(
            code=f"{OWNER}.{code}",
            message=message,
            details=details,
        )


@dataclass(frozen=True, slots=True)
class CsatReportQuery:
    date_from: datetime | None = None
    date_to: datetime | None = None
    rating: int | None = None
    source_type: CsatSourceType | None = None
    agent_person_id: UUID | None = None
    service_team_id: UUID | None = None
    source_channel: str | None = None
    status: CsatRequestStatus | None = CsatRequestStatus.submitted
    page: int = 1
    per_page: int | None = 50


@dataclass(frozen=True, slots=True)
class CsatReportSummary:
    total: int
    submitted: int
    average_rating: Decimal | None
    rating_counts: dict[int, int]
    positive_count: int
    positive_percentage: Decimal | None
    response_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class CsatTrendPoint:
    period: str
    requested: int
    submitted: int
    positive: int
    average_rating: Decimal | None


@dataclass(frozen=True, slots=True)
class CsatFilterOption:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class InboxCsatPolicy:
    enabled: bool = True
    style: str = "stars"
    message: str = "How would you rate this conversation?"


def inbox_survey_policy(
    db: Session, conversation: InboxConversation
) -> InboxCsatPolicy:
    route = (
        db.query(TeamInboxChannelRoute)
        .filter(
            TeamInboxChannelRoute.is_active.is_(True),
            TeamInboxChannelRoute.channel_type == conversation.channel_type,
            TeamInboxChannelRoute.service_team_id
            == conversation.primary_service_team_id,
        )
        .order_by(TeamInboxChannelRoute.priority.asc(), TeamInboxChannelRoute.id.asc())
        .first()
    )
    metadata = dict(route.metadata_ or {}) if route is not None else {}
    tags = {
        str(value).strip().casefold()
        for value in ((conversation.metadata_ or {}).get("tags") or [])
        if str(value).strip()
    }
    include = {
        str(value).strip().casefold()
        for value in (metadata.get("csat_include_labels") or [])
        if str(value).strip()
    }
    exclude = {
        str(value).strip().casefold()
        for value in (metadata.get("csat_exclude_labels") or [])
        if str(value).strip()
    }
    enabled = bool(metadata.get("csat_enabled", True))
    if include and not tags.intersection(include):
        enabled = False
    if exclude and tags.intersection(exclude):
        enabled = False
    return InboxCsatPolicy(
        enabled=enabled,
        style="emoji" if metadata.get("csat_style") == "emoji" else "stars",
        message=str(
            metadata.get("csat_message") or "How would you rate this conversation?"
        )[:200],
    )


@dataclass(frozen=True, slots=True)
class CustomerCsatSnapshot:
    customer_id: UUID | None
    customer_account_id: UUID | None
    customer_person_id: UUID | None
    customer_display_name: str | None
    customer_email: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_comment(comment: str | None) -> str | None:
    value = str(comment or "").strip()
    return value[:2000] or None


def _validate_rating(rating: int) -> int:
    clean = int(rating)
    if clean < 1 or clean > 5:
        raise SupportCsatError("invalid_rating", "Rating must be between 1 and 5.")
    return clean


def _customer_snapshot(
    db: Session,
    *,
    subscriber_id: UUID | None,
    customer_account_id: UUID | None,
    customer_person_id: UUID | None,
) -> CustomerCsatSnapshot:
    customer_id = subscriber_id or customer_account_id or customer_person_id
    subscriber = db.get(Subscriber, customer_id) if customer_id else None
    display_name = None
    email = None
    if subscriber is not None:
        display_name = (
            subscriber.display_name
            or subscriber.company_name
            or " ".join(
                part for part in (subscriber.first_name, subscriber.last_name) if part
            ).strip()
            or None
        )
        email = subscriber.email
    return CustomerCsatSnapshot(
        customer_id=customer_id,
        customer_account_id=customer_account_id,
        customer_person_id=customer_person_id,
        customer_display_name=display_name,
        customer_email=email,
    )


def _agent_name(db: Session, person_id: UUID | None) -> str | None:
    if person_id is None:
        return None
    user = (
        db.query(SystemUser)
        .filter(
            or_(SystemUser.id == person_id, SystemUser.person_party_id == person_id)
        )
        .one_or_none()
    )
    if user is None:
        return None
    return (
        user.display_name
        or " ".join(part for part in (user.first_name, user.last_name) if part).strip()
        or user.email
    )


def _team_name(db: Session, team_id: UUID | None) -> str | None:
    team = db.get(ServiceTeam, team_id) if team_id else None
    return team.name if team is not None else None


def _latest_active_assignment(
    db: Session, conversation_id: UUID
) -> InboxConversationAssignment | None:
    return (
        db.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == conversation_id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .order_by(InboxConversationAssignment.assigned_at.desc())
        .first()
    )


def _existing_request(
    db: Session,
    *,
    source_type: CsatSourceType,
    source_id: UUID,
    resolution_cycle_key: str,
) -> SupportCsatRequest | None:
    return (
        db.query(SupportCsatRequest)
        .filter(SupportCsatRequest.source_type == source_type.value)
        .filter(SupportCsatRequest.source_id == source_id)
        .filter(SupportCsatRequest.resolution_cycle_key == resolution_cycle_key)
        .one_or_none()
    )


def _latest_request(
    db: Session,
    *,
    source_type: CsatSourceType,
    source_id: UUID,
) -> SupportCsatRequest | None:
    return (
        db.query(SupportCsatRequest)
        .filter(SupportCsatRequest.source_type == source_type.value)
        .filter(SupportCsatRequest.source_id == source_id)
        .order_by(SupportCsatRequest.requested_at.desc(), SupportCsatRequest.id.desc())
        .first()
    )


def _latest_pending_request(
    db: Session,
    *,
    source_type: CsatSourceType,
    source_id: UUID,
) -> SupportCsatRequest | None:
    return (
        db.query(SupportCsatRequest)
        .filter(SupportCsatRequest.source_type == source_type.value)
        .filter(SupportCsatRequest.source_id == source_id)
        .filter(SupportCsatRequest.status == CsatRequestStatus.pending.value)
        .order_by(SupportCsatRequest.requested_at.desc(), SupportCsatRequest.id.desc())
        .with_for_update()
        .first()
    )


def _create_request(
    db: Session,
    *,
    source_type: CsatSourceType,
    source_id: UUID,
    source_reference: str | None,
    resolution_cycle_key: str,
    resolution_at: datetime,
    customer: CustomerCsatSnapshot,
    agent_person_id: UUID | None,
    service_team_id: UUID | None,
    source_channel: str | None = None,
) -> SupportCsatRequest:
    existing = _existing_request(
        db,
        source_type=source_type,
        source_id=source_id,
        resolution_cycle_key=resolution_cycle_key,
    )
    if existing is not None:
        return existing
    request = SupportCsatRequest(
        source_type=source_type.value,
        source_id=source_id,
        source_reference=source_reference,
        resolution_cycle_key=resolution_cycle_key,
        resolution_at=resolution_at,
        customer_id=customer.customer_id,
        customer_account_id=customer.customer_account_id,
        customer_person_id=customer.customer_person_id,
        customer_display_name=customer.customer_display_name,
        customer_email=customer.customer_email,
        agent_person_id=agent_person_id,
        agent_display_name=_agent_name(db, agent_person_id),
        service_team_id=service_team_id,
        service_team_name=_team_name(db, service_team_id),
        source_channel=(str(source_channel or "").strip() or None),
        status=CsatRequestStatus.pending.value,
        requested_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(request)
    db.flush()
    return request


def ensure_ticket_request(
    db: Session,
    ticket: Ticket,
    *,
    force_new_cycle: bool = False,
    resolution_at: datetime | None = None,
) -> SupportCsatRequest | None:
    if ticket.status != TicketStatus.closed.value:
        return None
    customer_id = ticket.subscriber_id or ticket.customer_account_id
    if customer_id is None:
        return None
    meta = dict(ticket.metadata_ or {})
    if force_new_cycle or not meta.get(LEGACY_TICKET_CYCLE_KEY):
        meta[LEGACY_TICKET_CYCLE_KEY] = (
            f"support-ticket:{ticket.id}:resolution:{uuid4()}"
        )
        meta.pop("csat", None)
        ticket.metadata_ = meta
    cycle_key = str(meta[LEGACY_TICKET_CYCLE_KEY])
    return _create_request(
        db,
        source_type=CsatSourceType.support_ticket,
        source_id=ticket.id,
        source_reference=ticket.number,
        resolution_cycle_key=cycle_key,
        resolution_at=resolution_at or ticket.closed_at or ticket.resolved_at or _now(),
        customer=_customer_snapshot(
            db,
            subscriber_id=ticket.subscriber_id,
            customer_account_id=ticket.customer_account_id,
            customer_person_id=ticket.customer_person_id,
        ),
        agent_person_id=ticket.assigned_to_person_id,
        service_team_id=ticket.service_team_id,
    )


def queue_request_notification(
    db: Session,
    request: SupportCsatRequest,
) -> None:
    if request.source_type != CsatSourceType.support_ticket.value:
        return
    if request.customer_id is None:
        return
    from app.models.notification import NotificationChannel
    from app.services import customer_experience_communications

    reference = request.source_reference or str(request.source_id)
    customer_experience_communications.request_update(
        db,
        subscriber_id=request.customer_id,
        event_type="support_csat_request",
        subject=f"Rate support ticket {reference}",
        body=(
            f"Your support ticket {reference} has been resolved. "
            f"Share your rating here: /portal/support/{request.source_id}"
        ),
        metadata={
            "type": "support_csat",
            "csat_request_id": str(request.id),
            "source_type": request.source_type,
            "source_id": str(request.source_id),
        },
        dedupe_key=f"support-csat-request:{request.id}",
        default_channels=(
            NotificationChannel.email,
            NotificationChannel.whatsapp,
            NotificationChannel.push,
        ),
    )


def ensure_inbox_request(
    db: Session,
    conversation: InboxConversation,
    *,
    transition_event_id: UUID,
    resolution_at: datetime,
    actor_person_id: UUID | None,
) -> SupportCsatRequest | None:
    if conversation.status != InboxConversationStatus.resolved.value:
        return None
    if conversation.subscriber_id is None:
        return None
    if not inbox_survey_policy(db, conversation).enabled:
        return None
    assignment = _latest_active_assignment(db, conversation.id)
    agent_person_id = (
        assignment.person_id if assignment is not None else actor_person_id
    )
    service_team_id = (
        assignment.service_team_id
        if assignment is not None
        else conversation.primary_service_team_id
    )
    return _create_request(
        db,
        source_type=CsatSourceType.inbox_conversation,
        source_id=conversation.id,
        source_reference=str(conversation.id),
        resolution_cycle_key=f"inbox-status-transition:{transition_event_id}",
        resolution_at=resolution_at,
        customer=_customer_snapshot(
            db,
            subscriber_id=conversation.subscriber_id,
            customer_account_id=None,
            customer_person_id=None,
        ),
        agent_person_id=agent_person_id,
        service_team_id=service_team_id,
        source_channel=conversation.channel_type,
    )


def pending_ticket_request(db: Session, ticket: Ticket) -> SupportCsatRequest | None:
    return _latest_pending_request(
        db, source_type=CsatSourceType.support_ticket, source_id=ticket.id
    )


def latest_ticket_request(db: Session, ticket: Ticket) -> SupportCsatRequest | None:
    return _latest_request(
        db, source_type=CsatSourceType.support_ticket, source_id=ticket.id
    )


def submit_ticket_rating(
    db: Session,
    ticket: Ticket,
    *,
    rating: int,
    comment: str | None = None,
    submitted_by: str | None = None,
    channel: str = "portal",
) -> SupportCsatRequest:
    if ticket.status != TicketStatus.closed.value:
        raise SupportCsatError(
            "not_eligible", "You can rate support once the ticket is closed."
        )
    request = pending_ticket_request(db, ticket)
    if request is None:
        latest = latest_ticket_request(db, ticket)
        if latest is not None and latest.status == CsatRequestStatus.submitted.value:
            raise SupportCsatError(
                "already_submitted", "This support rating has already been submitted."
            )
        request = ensure_ticket_request(db, ticket)
    if request is None:
        raise SupportCsatError(
            "request_unavailable", "This support rating request is unavailable."
        )
    return submit_request(
        db,
        request,
        rating=rating,
        comment=comment,
        submitted_by=submitted_by,
        channel=channel,
        ticket=ticket,
    )


def submit_inbox_rating(
    db: Session,
    conversation: InboxConversation,
    *,
    rating: int,
    comment: str | None = None,
    submitted_by: str | None = None,
    channel: str = "chat_widget",
) -> SupportCsatRequest:
    if conversation.status != InboxConversationStatus.resolved.value:
        raise SupportCsatError(
            "not_eligible", "Only resolved conversations can be rated."
        )
    request = _latest_pending_request(
        db,
        source_type=CsatSourceType.inbox_conversation,
        source_id=conversation.id,
    )
    if request is None:
        latest = _latest_request(
            db,
            source_type=CsatSourceType.inbox_conversation,
            source_id=conversation.id,
        )
        if latest is not None and latest.status == CsatRequestStatus.submitted.value:
            raise SupportCsatError(
                "already_submitted", "This support rating has already been submitted."
            )
        request = _create_request(
            db,
            source_type=CsatSourceType.inbox_conversation,
            source_id=conversation.id,
            source_reference=str(conversation.id),
            resolution_cycle_key=f"inbox-legacy-resolution:{conversation.id}:{uuid4()}",
            resolution_at=conversation.updated_at or _now(),
            customer=_customer_snapshot(
                db,
                subscriber_id=conversation.subscriber_id,
                customer_account_id=None,
                customer_person_id=None,
            ),
            agent_person_id=None,
            service_team_id=conversation.primary_service_team_id,
            source_channel=conversation.channel_type,
        )
    return submit_request(
        db,
        request,
        rating=rating,
        comment=comment,
        submitted_by=submitted_by,
        channel=channel,
        conversation=conversation,
    )


def submit_request(
    db: Session,
    request: SupportCsatRequest,
    *,
    rating: int,
    comment: str | None,
    submitted_by: str | None,
    channel: str,
    ticket: Ticket | None = None,
    conversation: InboxConversation | None = None,
) -> SupportCsatRequest:
    if request.status != CsatRequestStatus.pending.value:
        raise SupportCsatError(
            "already_submitted", "This support rating has already been submitted."
        )
    now = _now()
    request.rating = _validate_rating(rating)
    request.comment = _clean_comment(comment)
    request.submitted_at = now
    request.submitted_by = str(submitted_by or "").strip() or None
    request.submission_channel = channel
    request.status = CsatRequestStatus.submitted.value
    request.updated_at = now
    if ticket is not None:
        meta = dict(ticket.metadata_ or {})
        meta["csat"] = {
            "rating": request.rating,
            "comment": request.comment,
            "at": now.isoformat(),
            "request_id": str(request.id),
            "resolution_cycle_key": request.resolution_cycle_key,
        }
        ticket.metadata_ = meta
        db.add(ticket)
    if conversation is not None:
        metadata = dict(conversation.metadata_ or {})
        metadata["csat"] = {
            "rating": request.rating,
            "comment": request.comment,
            "actor": request.submitted_by,
            "rated_at": now.isoformat(),
            "request_id": str(request.id),
            "resolution_cycle_key": request.resolution_cycle_key,
        }
        conversation.metadata_ = metadata
        db.add(conversation)
    db.add(request)
    db.flush()
    return request


def report_summary(db: Session, query: CsatReportQuery) -> CsatReportSummary:
    scoped = _filtered_query(
        db, replace(query, rating=None, status=None, page=1, per_page=None)
    ).order_by(None)
    total = int(scoped.count())
    submitted_filter = scoped.filter(
        SupportCsatRequest.status == CsatRequestStatus.submitted.value
    )
    submitted = int(submitted_filter.count())
    average_value = submitted_filter.with_entities(
        func.avg(SupportCsatRequest.rating)
    ).scalar()
    counts = {
        int(score): int(count)
        for score, count in submitted_filter.with_entities(
            SupportCsatRequest.rating,
            func.count(SupportCsatRequest.id),
        )
        .filter(SupportCsatRequest.rating.isnot(None))
        .group_by(SupportCsatRequest.rating)
        .all()
    }
    positive_count = counts.get(4, 0) + counts.get(5, 0)
    return CsatReportSummary(
        total=total,
        submitted=submitted,
        average_rating=Decimal(str(average_value))
        if average_value is not None
        else None,
        rating_counts={score: counts.get(score, 0) for score in range(1, 6)},
        positive_count=positive_count,
        positive_percentage=(
            Decimal(positive_count * 100) / Decimal(submitted) if submitted else None
        ),
        response_rate=(Decimal(submitted * 100) / Decimal(total) if total else None),
    )


def report_trend(db: Session, query: CsatReportQuery) -> tuple[CsatTrendPoint, ...]:
    """Return daily request, response, positive-score, and average-rating points."""

    scoped = _filtered_query(
        db, replace(query, rating=None, status=None, page=1, per_page=None)
    ).order_by(None)
    period = func.date(SupportCsatRequest.resolution_at)
    rows = (
        scoped.with_entities(
            period.label("period"),
            func.count(SupportCsatRequest.id).label("requested"),
            func.sum(
                func.cast(
                    SupportCsatRequest.status == CsatRequestStatus.submitted.value,
                    Integer,
                )
            ).label("submitted"),
            func.sum(func.cast(SupportCsatRequest.rating.in_((4, 5)), Integer)).label(
                "positive"
            ),
            func.avg(SupportCsatRequest.rating).label("average_rating"),
        )
        .group_by(period)
        .order_by(period)
        .all()
    )
    return tuple(
        CsatTrendPoint(
            period=str(row.period),
            requested=int(row.requested or 0),
            submitted=int(row.submitted or 0),
            positive=int(row.positive or 0),
            average_rating=(
                Decimal(str(row.average_rating))
                if row.average_rating is not None
                else None
            ),
        )
        for row in rows
    )


def report_filter_options(
    db: Session,
) -> tuple[tuple[CsatFilterOption, ...], tuple[CsatFilterOption, ...], tuple[str, ...]]:
    """Return human-readable selectors from immutable CSAT snapshots."""

    agents = tuple(
        CsatFilterOption(id=person_id, name=str(name or person_id))
        for person_id, name in db.query(
            SupportCsatRequest.agent_person_id,
            func.max(SupportCsatRequest.agent_display_name),
        )
        .filter(SupportCsatRequest.agent_person_id.isnot(None))
        .group_by(SupportCsatRequest.agent_person_id)
        .order_by(func.max(SupportCsatRequest.agent_display_name).asc())
        .all()
    )
    teams = tuple(
        CsatFilterOption(id=team_id, name=str(name or team_id))
        for team_id, name in db.query(
            SupportCsatRequest.service_team_id,
            func.max(SupportCsatRequest.service_team_name),
        )
        .filter(SupportCsatRequest.service_team_id.isnot(None))
        .group_by(SupportCsatRequest.service_team_id)
        .order_by(func.max(SupportCsatRequest.service_team_name).asc())
        .all()
    )
    channels = tuple(
        str(value)
        for (value,) in db.query(SupportCsatRequest.source_channel)
        .filter(SupportCsatRequest.source_channel.isnot(None))
        .distinct()
        .order_by(SupportCsatRequest.source_channel.asc())
        .all()
    )
    return agents, teams, channels


def report_rows(db: Session, query: CsatReportQuery) -> list[SupportCsatRequest]:
    rows = _filtered_query(db, query).order_by(
        SupportCsatRequest.submitted_at.desc().nullslast(),
        SupportCsatRequest.resolution_at.desc(),
        SupportCsatRequest.id.desc(),
    )
    if query.per_page is None:
        return rows.all()
    return apply_pagination(
        rows, query.per_page, max(0, (query.page - 1) * query.per_page)
    ).all()


def report_total(db: Session, query: CsatReportQuery) -> int:
    return int(_filtered_query(db, query).order_by(None).count())


def _filtered_query(db: Session, query: CsatReportQuery):
    rows = db.query(SupportCsatRequest)
    if query.status is not None:
        rows = rows.filter(SupportCsatRequest.status == query.status.value)
    if query.date_from is not None:
        rows = rows.filter(SupportCsatRequest.resolution_at >= query.date_from)
    if query.date_to is not None:
        rows = rows.filter(SupportCsatRequest.resolution_at < query.date_to)
    if query.rating is not None:
        rows = rows.filter(SupportCsatRequest.rating == query.rating)
    if query.source_type is not None:
        rows = rows.filter(SupportCsatRequest.source_type == query.source_type.value)
    if query.agent_person_id is not None:
        rows = rows.filter(SupportCsatRequest.agent_person_id == query.agent_person_id)
    if query.service_team_id is not None:
        rows = rows.filter(SupportCsatRequest.service_team_id == query.service_team_id)
    if query.source_channel:
        rows = rows.filter(SupportCsatRequest.source_channel == query.source_channel)
    return rows
