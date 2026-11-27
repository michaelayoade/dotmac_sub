"""Read-only customer connectivity projection for an infrastructure scope.

Topology owns membership, RADIUS owns current proof of life, the outage
interval ledger owns confirmed downtime, and Support owns ticket state. A
missing session alone never becomes confirmed provider downtime.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    CustomerOutageInterval,
    NetworkDevice,
    PopSite,
)
from app.models.support import Ticket
from app.models.usage import RadiusAccountingSession
from app.services.customer_service_state import (
    OPEN_INFRASTRUCTURE_TICKET_STATUSES,
    is_infrastructure_down_ticket,
)
from app.services.customer_support_links import ticket_customer_linked_ids
from app.services.topology.affected import affected_customers
from app.services.topology.health_classifier import online_subscription_ids
from app.services.topology.last_mile import diagnose_many


class ConnectivityScope(StrEnum):
    basestation = "basestation"
    cabinet = "cabinet"
    node = "node"


class ConnectivityFilter(StrEnum):
    all = "all"
    no_session = "no_session"
    no_ticket = "no_ticket"


@dataclass(frozen=True, slots=True)
class ScopeChoice:
    id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class ConnectivityRow:
    subscription_id: uuid.UUID
    customer_name: str
    has_live_session: bool
    verdict: str | None
    medium: str | None
    signal_dbm: float | None
    downtime_started_at: datetime | None
    downtime_seconds: int | None
    downtime_quality: str | None
    ticket_id: uuid.UUID | None
    ticket_number: str | None
    ticket_status: str | None


@dataclass(frozen=True, slots=True)
class ConnectivityPage:
    scope: ConnectivityScope
    scope_id: uuid.UUID
    scope_name: str
    equipment_status: str | None
    total_customers: int
    without_session: int
    open_service_tickets: int
    rows: tuple[ConnectivityRow, ...]
    page: int
    has_next: bool


def search_scopes(
    db: Session, *, scope: ConnectivityScope, query: str, limit: int = 25
) -> tuple[ScopeChoice, ...]:
    """Find active assets, including healthy ones, without a live fleet sweep."""
    model = {
        ConnectivityScope.basestation: PopSite,
        ConnectivityScope.cabinet: FdhCabinet,
        ConnectivityScope.node: NetworkDevice,
    }[scope]
    statement = select(model.id, model.name).where(model.is_active.is_(True))
    term = query.strip()
    if term:
        statement = statement.where(model.name.ilike(f"%{term}%"))
    statement = statement.order_by(model.name, model.id).limit(limit)
    return tuple(ScopeChoice(id=row.id, name=row.name) for row in db.execute(statement))


def customer_connectivity(
    db: Session,
    *,
    scope: ConnectivityScope,
    scope_id: uuid.UUID,
    status_filter: ConnectivityFilter = ConnectivityFilter.all,
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,
    include_tickets: bool = False,
) -> ConnectivityPage | None:
    """All mapped customers in a BTS, cabinet, or node, regardless of health."""
    model = {
        ConnectivityScope.basestation: PopSite,
        ConnectivityScope.cabinet: FdhCabinet,
        ConnectivityScope.node: NetworkDevice,
    }[scope]
    asset = db.get(model, scope_id)
    if asset is None or not asset.is_active:
        return None
    page_size = max(1, min(page_size, 50))
    instant = now or datetime.now(UTC)
    impact = affected_customers(
        db,
        basestation=asset if scope == ConnectivityScope.basestation else None,
        fdh=asset if scope == ConnectivityScope.cabinet else None,
        node=asset if scope == ConnectivityScope.node else None,
    )
    subscriptions = sorted(impact["subscriptions"], key=lambda sub: str(sub.id))
    subscription_ids = {sub.id for sub in subscriptions}
    live_ids = online_subscription_ids(db, subscription_ids, now=instant)
    subscriber_ids = {sub.subscriber_id for sub in subscriptions if sub.subscriber_id}

    intervals: dict[uuid.UUID, CustomerOutageInterval] = {}
    if subscription_ids:
        for interval in db.scalars(
            select(CustomerOutageInterval)
            .where(
                CustomerOutageInterval.subscription_id.in_(subscription_ids),
                CustomerOutageInterval.ended_at.is_(None),
                CustomerOutageInterval.finalized_at.is_(None),
                CustomerOutageInterval.state == "confirmed_unavailable",
            )
            .order_by(CustomerOutageInterval.started_at)
        ):
            intervals.setdefault(interval.subscription_id, interval)

    # Imported accounting stops are useful for investigation, but they can
    # lag. Keep their timestamps explicitly estimated, never ledger-confirmed.
    last_disconnects: dict[uuid.UUID, datetime] = {}
    if subscription_ids:
        for subscription_id, ended_at in db.execute(
            select(
                RadiusAccountingSession.subscription_id,
                func.max(RadiusAccountingSession.session_end),
            )
            .where(
                RadiusAccountingSession.subscription_id.in_(subscription_ids),
                RadiusAccountingSession.session_end.is_not(None),
                RadiusAccountingSession.session_end <= instant,
            )
            .group_by(RadiusAccountingSession.subscription_id)
        ):
            if subscription_id is not None and ended_at is not None:
                last_disconnects[subscription_id] = ended_at

    tickets: dict[uuid.UUID, Ticket] = {}
    if include_tickets and subscriber_ids:
        rows = db.scalars(
            select(Ticket)
            .where(
                Ticket.is_active.is_(True),
                Ticket.merged_into_ticket_id.is_(None),
                Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES),
                or_(
                    Ticket.subscriber_id.in_(subscriber_ids),
                    Ticket.customer_account_id.in_(subscriber_ids),
                    Ticket.customer_person_id.in_(subscriber_ids),
                ),
            )
            .order_by(Ticket.created_at.desc(), Ticket.id)
        )
        for ticket in rows:
            if not is_infrastructure_down_ticket(ticket):
                continue
            for subscriber_id in ticket_customer_linked_ids(ticket):
                if subscriber_id in subscriber_ids:
                    tickets.setdefault(subscriber_id, ticket)

    selected = [
        sub
        for sub in subscriptions
        if (status_filter != ConnectivityFilter.no_session or sub.id not in live_ids)
        and (
            status_filter != ConnectivityFilter.no_ticket
            or (sub.id not in live_ids and sub.subscriber_id not in tickets)
        )
    ]
    selected.sort(key=lambda sub: (sub.id in live_ids, str(sub.id)))
    offset = (page - 1) * page_size
    visible = selected[offset : offset + page_size]
    verdicts = diagnose_many(db, [sub.id for sub in visible], now=instant)
    result_rows: list[ConnectivityRow] = []
    for sub in visible:
        diagnosis = verdicts.get(sub.id, {})
        interval = intervals.get(sub.id) if sub.id not in live_ids else None
        ticket = tickets.get(sub.subscriber_id)
        started = (
            interval.started_at
            if interval
            else last_disconnects.get(sub.id)
            if sub.id not in live_ids
            else None
        )
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        result_rows.append(
            ConnectivityRow(
                subscription_id=sub.id,
                customer_name=(
                    f"{sub.subscriber.first_name} {sub.subscriber.last_name}"
                    if sub.subscriber
                    else "Unknown customer"
                ),
                has_live_session=sub.id in live_ids,
                verdict=diagnosis.get("verdict"),
                medium=diagnosis.get("medium"),
                signal_dbm=diagnosis.get("signal_dbm"),
                downtime_started_at=started,
                downtime_seconds=(
                    max(0, int((instant - started).total_seconds()))
                    if started
                    else None
                ),
                downtime_quality=(
                    interval.quality
                    if interval
                    else "estimated_from_last_disconnect"
                    if started
                    else None
                ),
                ticket_id=ticket.id if ticket else None,
                ticket_number=ticket.number if ticket else None,
                ticket_status=ticket.status if ticket else None,
            )
        )
    return ConnectivityPage(
        scope=scope,
        scope_id=scope_id,
        scope_name=asset.name,
        equipment_status=getattr(asset, "live_status", None),
        total_customers=len(subscriptions),
        without_session=len(subscription_ids - live_ids),
        open_service_tickets=len(tickets),
        rows=tuple(result_rows),
        page=page,
        has_next=offset + page_size < len(selected),
    )
