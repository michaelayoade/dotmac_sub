"""Pre-create validation for native support tickets.

This ports CRM's deterministic ISP ticket checks into sub while leaving CRM's
generic pre-create automation-rule engine for the later automation/workqueue
module absorption.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.schemas.support import TicketCreate
from app.services.customer_support_links import (
    ticket_customer_any_link_filter,
    ticket_has_customer_link,
    ticket_links_customer,
    ticket_unlinked_customer_filter,
)

_OPEN_STATUSES = {
    TicketStatus.new.value,
    TicketStatus.open.value,
    TicketStatus.pending.value,
    TicketStatus.waiting_on_customer.value,
    TicketStatus.lastmile_rerun.value,
    TicketStatus.site_under_construction.value,
    TicketStatus.on_hold.value,
    TicketStatus.pending_confirmation.value,
}

# Duplicate-candidate scoring thresholds (ported from CRM's ticket service).
DUPLICATE_WARNING_THRESHOLD = 55
DUPLICATE_LIKELY_THRESHOLD = 80
DUPLICATE_CANDIDATE_LIMIT = 20

_SUBSCRIBER_REQUIRED_TICKET_TYPES = {
    "bandwidth complaint",
    "customer link disconnection",
    "customer realignment",
    "dns/domain issue",
    "lan troubleshooting",
    "power optimization (if specific to customer premises)",
    "slow browsing / intermittent connectivity",
    "router troubleshooting",
    "router replacement",
    "call down support",
}

_BASE_STATION_REQUIRED_TICKET_TYPES = {
    "cedar view (likely a site/location issue)",
    "core link disconnection",
    "dell server down",
    "multiple cabinet disconnection",
    "multiple customer link disconnection",
    "nextcloud service down",
    "subscriber-system issue",
    "access point outage",
    "multiple cabinet link disconnection",
    "bts outage",
}


def ticket_type_requires_subscriber(ticket_type: str | None) -> bool:
    return _normalize_ticket_type(ticket_type) in _SUBSCRIBER_REQUIRED_TICKET_TYPES


def subscriber_required_ticket_types() -> list[str]:
    return sorted(_SUBSCRIBER_REQUIRED_TICKET_TYPES)


def ticket_type_requires_base_station(ticket_type: str | None) -> bool:
    return _normalize_ticket_type(ticket_type) in _BASE_STATION_REQUIRED_TICKET_TYPES


def base_station_required_ticket_types() -> list[str]:
    return sorted(_BASE_STATION_REQUIRED_TICKET_TYPES)


def validate_ticket_creation(db: Session, payload: TicketCreate) -> None:
    """Reject invalid user-created tickets before any row is inserted."""
    if (
        _created_by_is_customer(db, payload)
        and not str(payload.ticket_type or "").strip()
    ):
        raise HTTPException(status_code=400, detail="Ticket type is required.")

    if ticket_type_requires_subscriber(payload.ticket_type) and not (
        payload.subscriber_id
        or payload.customer_account_id
        or payload.customer_person_id
    ):
        raise HTTPException(
            status_code=400,
            detail="Subscriber is required for the selected ticket type.",
        )

    metadata = payload.metadata_ if isinstance(payload.metadata_, dict) else {}
    base_station_details = str(metadata.get("base_station_details") or "").strip()
    if (
        ticket_type_requires_base_station(payload.ticket_type)
        and not base_station_details
    ):
        raise HTTPException(
            status_code=400,
            detail="Base station details are required for the selected ticket type.",
        )

    context = build_pre_create_context(db, payload)
    duplicate_ticket_id = context.get("duplicate_ticket_id")
    if duplicate_ticket_id and metadata.get("duplicate_block") is True:
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate open ticket already exists (blocking ticket: {duplicate_ticket_id})",
        )


def build_pre_create_context(db: Session, payload: TicketCreate) -> dict[str, Any]:
    """Build the CRM-compatible duplicate context for future pre-create rules."""
    metadata = payload.metadata_ if isinstance(payload.metadata_, dict) else {}
    duplicate_override = metadata.get("duplicate_override") is True
    context: dict[str, Any] = {
        "ticket_type": payload.ticket_type,
        "customer_person_id": str(payload.customer_person_id)
        if payload.customer_person_id
        else None,
        "subscriber_id": str(payload.subscriber_id) if payload.subscriber_id else None,
        "priority": str(payload.priority) if payload.priority else None,
        "channel": payload.channel.value if payload.channel else None,
        "title": payload.title,
        "duplicate_override": duplicate_override,
    }

    open_tickets = _open_tickets_for_payload(db, payload)
    context["open_ticket_types"] = [
        row.ticket_type for row in open_tickets if row.ticket_type
    ]
    context["open_ticket_count"] = len(open_tickets)

    if payload.ticket_type:
        normalized_type = _normalize_ticket_type(payload.ticket_type)
        duplicate = next(
            (
                row
                for row in open_tickets
                if _normalize_ticket_type(row.ticket_type) == normalized_type
            ),
            None,
        )
        if duplicate and not duplicate_override:
            context["duplicate_ticket_id"] = str(duplicate.id)

    return context


def _open_tickets_for_payload(db: Session, payload: TicketCreate) -> list[Ticket]:
    customer_ids = (
        payload.subscriber_id,
        payload.customer_account_id,
        payload.customer_person_id,
    )
    if not any(customer_ids):
        return []

    return (
        db.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(_OPEN_STATUSES))
        .filter(ticket_customer_any_link_filter(Ticket, customer_ids))
        .all()
    )


def _normalize_ticket_type(ticket_type: str | None) -> str:
    if not isinstance(ticket_type, str):
        return ""
    return ticket_type.strip().lower()


def _created_by_is_customer(db: Session, payload: TicketCreate) -> bool:
    if not payload.created_by_person_id:
        return False
    return db.get(Subscriber, payload.created_by_person_id) is not None


@dataclass(frozen=True)
class TicketDuplicateInput:
    title: str | None = None
    description: str | None = None
    exclude_ticket_id: UUID | str | None = None
    subscriber_id: UUID | str | None = None
    customer_account_id: UUID | str | None = None
    customer_person_id: UUID | str | None = None
    lead_id: UUID | str | None = None
    ticket_type: str | None = None
    base_station_details: str | None = None
    tags: list[str] | None = None
    region: str | None = None


@dataclass(frozen=True)
class TicketDuplicateMatch:
    ticket_id: str
    number: str | None
    title: str
    status: str
    ticket_type: str | None
    created_at: datetime | None
    updated_at: datetime | None
    score: int
    confidence: str
    reasons: list[str]
    subscriber_label: str | None = None
    customer_label: str | None = None

    @property
    def reference(self) -> str:
        return self.number or self.ticket_id

    def as_dict(self) -> dict[str, object]:
        return {
            "ticket_id": self.ticket_id,
            "number": self.number,
            "reference": self.reference,
            "title": self.title,
            "status": self.status,
            "ticket_type": self.ticket_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "score": self.score,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "subscriber_label": self.subscriber_label,
            "customer_label": self.customer_label,
            "url": f"/admin/support/tickets/{self.reference}",
        }


@dataclass(frozen=True)
class TicketDuplicateResult:
    matches: list[TicketDuplicateMatch]

    @property
    def has_warning(self) -> bool:
        return bool(self.matches)

    @property
    def has_likely_duplicate(self) -> bool:
        return any(match.score >= DUPLICATE_LIKELY_THRESHOLD for match in self.matches)

    def as_dict(self) -> dict[str, object]:
        return {
            "has_warning": self.has_warning,
            "has_likely_duplicate": self.has_likely_duplicate,
            "matches": [match.as_dict() for match in self.matches],
        }


def _coerce_optional_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _normalize_duplicate_text(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9\s]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _text_similarity(left: str | None, right: str | None) -> float:
    left_norm = _normalize_duplicate_text(left)
    right_norm = _normalize_duplicate_text(right)
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    token_score = len(left_tokens & right_tokens) / max(
        len(left_tokens | right_tokens), 1
    )
    sequence_score = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(token_score, sequence_score)


def _normalize_duplicate_location(value: object | None) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return " ".join(normalized.split())


def _ticket_base_station_details(ticket: Ticket) -> str:
    metadata = ticket.metadata_ if isinstance(ticket.metadata_, dict) else {}
    return str(metadata.get("base_station_details") or "").strip()


def _subscriber_label(subscriber: Subscriber | None) -> str | None:
    if not subscriber:
        return None
    name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or subscriber.company_name
        or "Subscriber"
    )
    number = subscriber.subscriber_number
    return f"{name} ({number})" if number else name


def _duplicate_sort_ts(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def find_duplicate_ticket_candidates(
    db: Session, payload: TicketDuplicateInput
) -> TicketDuplicateResult:
    """Score open tickets that look like duplicates of the incoming payload.

    Ported from CRM's ``tickets.find_duplicate_ticket_candidates``, adapted to
    sub's ``support_tickets`` schema where subscriber/account/customer ids all
    reference ``subscribers`` rows.
    """
    exclude_ticket_id = _coerce_optional_uuid(payload.exclude_ticket_id)
    subscriber_id = _coerce_optional_uuid(payload.subscriber_id)
    customer_account_id = _coerce_optional_uuid(payload.customer_account_id)
    customer_person_id = _coerce_optional_uuid(payload.customer_person_id)
    lead_id = _coerce_optional_uuid(payload.lead_id)

    identity_filters: list[ColumnElement[bool]] = []
    customer_identity_filter = ticket_customer_any_link_filter(
        Ticket, (subscriber_id, customer_account_id, customer_person_id)
    )
    if subscriber_id or customer_account_id or customer_person_id:
        identity_filters.append(customer_identity_filter)
    if lead_id:
        identity_filters.append(Ticket.lead_id == lead_id)
    unassigned_issue_filters: list[ColumnElement[bool]] = [
        ticket_unlinked_customer_filter(Ticket),
        Ticket.lead_id.is_(None),
    ]
    if payload.ticket_type:
        unassigned_issue_filters.append(
            Ticket.ticket_type == payload.ticket_type.strip()
        )
    if (
        not identity_filters
        and not payload.ticket_type
        and not (payload.title or payload.description)
    ):
        return TicketDuplicateResult(matches=[])
    unassigned_candidate_filter = and_(*unassigned_issue_filters)
    candidate_filter = (
        or_(*identity_filters, unassigned_candidate_filter)
        if identity_filters
        else unassigned_candidate_filter
    )

    candidates_query = (
        db.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(candidate_filter)
        .filter(Ticket.status.in_(_OPEN_STATUSES))
    )
    if exclude_ticket_id:
        candidates_query = candidates_query.filter(Ticket.id != exclude_ticket_id)
    candidates = (
        candidates_query.order_by(Ticket.created_at.desc())
        .limit(DUPLICATE_CANDIDATE_LIMIT)
        .all()
    )

    incoming_title = payload.title or ""
    incoming_description = payload.description or ""
    incoming_type = (payload.ticket_type or "").strip().lower()
    incoming_base_station = _normalize_duplicate_location(payload.base_station_details)
    incoming_tags = {
        tag.strip().lower() for tag in payload.tags or [] if tag and tag.strip()
    }
    incoming_region = (payload.region or "").strip().lower()

    scored: list[tuple[int, list[str], Ticket]] = []
    for ticket in candidates:
        score = 0
        reasons: list[str] = []
        ticket_has_identity = ticket_has_customer_link(ticket) or bool(ticket.lead_id)
        existing_base_station = _normalize_duplicate_location(
            _ticket_base_station_details(ticket)
        )
        if incoming_base_station and existing_base_station:
            if incoming_base_station != existing_base_station:
                continue
            score += 35
            reasons.append("same base station")
        if subscriber_id and ticket.subscriber_id == subscriber_id:
            score += 40
            reasons.append("same subscriber")
        if customer_account_id and ticket.customer_account_id == customer_account_id:
            score += 20
            reasons.append("same account")
        if customer_person_id and ticket.customer_person_id == customer_person_id:
            score += 20
            reasons.append("same customer")
        if lead_id and ticket.lead_id == lead_id:
            score += 15
            reasons.append("same lead")

        title_similarity = _text_similarity(incoming_title, ticket.title)
        if title_similarity >= 0.75:
            score += 25
            reasons.append("very similar title")
        elif title_similarity >= 0.40:
            score += 15
            reasons.append("similar title")

        description_similarity = _text_similarity(
            incoming_description, ticket.description
        )
        if description_similarity >= 0.70:
            score += 20
            reasons.append("very similar description")
        elif description_similarity >= 0.40:
            score += 10
            reasons.append("similar description")

        if (
            incoming_type
            and _normalize_ticket_type(ticket.ticket_type) == incoming_type
        ):
            score += 15
            reasons.append("same ticket type")

        existing_tags = {
            str(tag).strip().lower() for tag in ticket.tags or [] if str(tag).strip()
        }
        if incoming_tags and existing_tags:
            tag_overlap = incoming_tags & existing_tags
            if tag_overlap:
                score += min(10, 4 * len(tag_overlap))
                reasons.append("matching tags")

        if incoming_region and (ticket.region or "").strip().lower() == incoming_region:
            score += 5
            reasons.append("same region")

        score += 10
        reasons.append("ticket is still active")
        subscriber_match = any(
            ticket_links_customer(ticket, customer_id)
            for customer_id in (subscriber_id, customer_account_id, customer_person_id)
        )
        if subscriber_match and score < DUPLICATE_WARNING_THRESHOLD:
            score = DUPLICATE_WARNING_THRESHOLD
            reasons.append("subscriber already has an active ticket")
        if not ticket_has_identity:
            strong_issue_match = (
                (
                    bool(
                        incoming_type
                        and _normalize_ticket_type(ticket.ticket_type) == incoming_type
                    )
                    and (title_similarity >= 0.40 or description_similarity >= 0.40)
                )
                or title_similarity >= 0.75
                or description_similarity >= 0.70
            )
            if not strong_issue_match:
                continue
            if score < DUPLICATE_WARNING_THRESHOLD:
                score = DUPLICATE_WARNING_THRESHOLD
            reasons.append("unassigned active ticket has a similar issue")

        if score < DUPLICATE_WARNING_THRESHOLD:
            continue
        scored.append((score, reasons, ticket))

    scored.sort(
        key=lambda item: (item[0], _duplicate_sort_ts(item[2].created_at)),
        reverse=True,
    )
    top = scored[:5]

    subscriber_ids = {
        ref
        for _, _, ticket in top
        for ref in (ticket.subscriber_id, ticket.customer_person_id)
        if ref
    }
    subscribers_by_id: dict[UUID, Subscriber] = {}
    if subscriber_ids:
        subscribers_by_id = {
            row.id: row
            for row in db.query(Subscriber)
            .filter(Subscriber.id.in_(subscriber_ids))
            .all()
        }

    matches = [
        TicketDuplicateMatch(
            ticket_id=str(ticket.id),
            number=ticket.number,
            title=ticket.title,
            status=ticket.status or "",
            ticket_type=ticket.ticket_type,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            score=min(score, 100),
            confidence="likely" if score >= DUPLICATE_LIKELY_THRESHOLD else "possible",
            reasons=reasons,
            subscriber_label=_subscriber_label(
                subscribers_by_id.get(ticket.subscriber_id)
                if ticket.subscriber_id
                else None
            ),
            customer_label=_subscriber_label(
                subscribers_by_id.get(ticket.customer_person_id)
                if ticket.customer_person_id
                else None
            ),
        )
        for score, reasons, ticket in top
    ]
    return TicketDuplicateResult(matches=matches)
