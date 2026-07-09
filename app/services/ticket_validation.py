"""Pre-create validation for native support tickets.

This ports CRM's deterministic ISP ticket checks into sub while leaving CRM's
generic pre-create automation-rule engine for the later automation/workqueue
module absorption.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.schemas.support import TicketCreate

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
    filters = []
    if payload.customer_person_id:
        filters.append(Ticket.customer_person_id == payload.customer_person_id)
    if payload.subscriber_id:
        filters.append(Ticket.subscriber_id == payload.subscriber_id)
    if payload.customer_account_id:
        filters.append(Ticket.customer_account_id == payload.customer_account_id)
    if not filters:
        return []

    query = (
        db.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(_OPEN_STATUSES))
    )
    if len(filters) == 1:
        query = query.filter(filters[0])
    else:
        from sqlalchemy import or_

        query = query.filter(or_(*filters))
    return query.all()


def _normalize_ticket_type(ticket_type: str | None) -> str:
    if not isinstance(ticket_type, str):
        return ""
    return ticket_type.strip().lower()


def _created_by_is_customer(db: Session, payload: TicketCreate) -> bool:
    if not payload.created_by_person_id:
        return False
    return db.get(Subscriber, payload.created_by_person_id) is not None
