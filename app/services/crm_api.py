from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.audit import AuditActorType, AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    CatalogOffer,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import DunningCase
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.models.network_monitoring import PopSite
from app.models.service_extension import ServiceExtension, ServiceExtensionEntry
from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.models.usage import AccountingStatus, RadiusAccountingSession

ACTIVE_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)
SUCCESSFUL_PAYMENT_STATUSES = (PaymentStatus.succeeded,)
ONLINE_FRESH_SECONDS = 24 * 60 * 60
_MISSING = object()


def coerce_subscriber_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def money(value: Any) -> float:
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def enum_value(value: Any) -> str | None:
    return getattr(value, "value", value)


def _uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def subscriber_name(subscriber: Subscriber) -> str:
    return (
        subscriber.name
        or subscriber.display_name
        or subscriber.email
        or str(subscriber.id)
    )


def address_text(
    subscriber: Subscriber, addresses: list[Address] | None = None
) -> str | None:
    address = None
    if addresses:
        address = (
            next((item for item in addresses if item.is_primary), None) or addresses[0]
        )
    if address is not None:
        parts = [
            address.address_line1,
            address.address_line2,
            address.city,
            address.region,
            address.postal_code,
            address.country_code,
        ]
    else:
        parts = [
            subscriber.address_line1,
            subscriber.address_line2,
            subscriber.city,
            subscriber.region,
            subscriber.postal_code,
            subscriber.country_code,
        ]
    rendered = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    return rendered or None


def location_label(db: Session, subscriber: Subscriber) -> str | None:
    if subscriber.pop_site_id:
        pop = db.get(PopSite, subscriber.pop_site_id)
        if pop:
            return pop.name
    return subscriber.city or subscriber.region or address_text(subscriber)


def location_id(db: Session, subscriber: Subscriber) -> str | None:
    if subscriber.pop_site_id:
        return str(subscriber.pop_site_id)
    label = location_label(db, subscriber)
    if not label:
        return None
    return f"address:{label.strip().lower()}"


def service_price(subscription: Subscription) -> float:
    if subscription.unit_price is not None:
        return money(subscription.unit_price)
    offer = subscription.offer
    if offer:
        recurring = next(
            (
                price
                for price in offer.prices
                if price.is_active and price.price_type == PriceType.recurring
            ),
            None,
        )
        if recurring:
            return money(recurring.amount)
    return 0.0


def service_speed(subscription: Subscription) -> str | None:
    offer = subscription.offer
    down = getattr(offer, "speed_download_mbps", None) if offer else None
    up = getattr(offer, "speed_upload_mbps", None) if offer else None
    if down and up:
        return f"{down}/{up} Mbps"
    if down:
        return f"{down} Mbps"
    profile = subscription.radius_profile
    if profile and (profile.download_speed or profile.upload_speed):
        profile_down = profile.download_speed
        profile_up = profile.upload_speed
        if profile_down and profile_up:
            return f"{profile_down}/{profile_up} Kbps"
        return f"{profile_down or profile_up} Kbps"
    return None


def service_record(subscription: Subscription) -> dict[str, Any]:
    offer = subscription.offer
    return {
        "service_id": str(subscription.id),
        "plan_name": offer.name if offer else subscription.service_description,
        "speed": service_speed(subscription),
        "status": enum_value(subscription.status),
        "activated_at": utc_iso(subscription.start_at),
        "price": service_price(subscription),
    }


def subscriber_services(db: Session, subscriber_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
            .options(
                joinedload(Subscription.offer).selectinload(CatalogOffer.prices),
                joinedload(Subscription.radius_profile),
            )
            .order_by(Subscription.created_at.desc())
        ).all()
    )
    return [service_record(row) for row in rows]


def services_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not subscriber_ids:
        return {}
    rows = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .options(
                joinedload(Subscription.offer).selectinload(CatalogOffer.prices),
                joinedload(Subscription.radius_profile),
            )
            .order_by(Subscription.created_at.desc())
        ).all()
    )
    mapped: dict[uuid.UUID, list[dict[str, Any]]] = {sid: [] for sid in subscriber_ids}
    for row in rows:
        mapped.setdefault(row.subscriber_id, []).append(service_record(row))
    return mapped


def _total_paid_query(db: Session, subscriber_id: uuid.UUID) -> Decimal:
    return Decimal(
        str(
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id == subscriber_id)
            .filter(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
            .filter(Payment.is_active.is_(True))
            .scalar()
            or 0
        )
    )


def _balance_query(db: Session, subscriber_id: uuid.UUID) -> Decimal:
    return Decimal(
        str(
            db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
            .filter(Invoice.account_id == subscriber_id)
            .filter(Invoice.status.in_(ACTIVE_INVOICE_STATUSES))
            .filter(Invoice.is_active.is_(True))
            .scalar()
            or 0
        )
    )


def _invoiced_until_query(db: Session, subscriber_id: uuid.UUID) -> datetime | None:
    return (
        db.query(func.max(Invoice.billing_period_end))
        .filter(Invoice.account_id == subscriber_id)
        .filter(Invoice.is_active.is_(True))
        .scalar()
    )


def _next_bill_date_query(db: Session, subscriber_id: uuid.UUID) -> datetime | None:
    return (
        db.query(func.min(Subscription.next_billing_at))
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.pending]
            )
        )
        .scalar()
    )


def _billing_start_query(db: Session, subscriber: Subscriber) -> datetime | None:
    if subscriber.account_start_date:
        return subscriber.account_start_date
    return (
        db.query(func.min(Subscription.start_at))
        .filter(Subscription.subscriber_id == subscriber.id)
        .scalar()
    )


def _blocked_date_query(db: Session, subscriber_id: uuid.UUID) -> datetime | None:
    lock_at = (
        db.query(func.max(EnforcementLock.created_at))
        .filter(EnforcementLock.subscriber_id == subscriber_id)
        .filter(
            EnforcementLock.reason.in_(
                [EnforcementReason.overdue, EnforcementReason.prepaid]
            )
        )
        .scalar()
    )
    dunning_at = (
        db.query(func.max(DunningCase.started_at))
        .filter(DunningCase.account_id == subscriber_id)
        .scalar()
    )
    values = [value for value in (lock_at, dunning_at) if value is not None]
    return max(values) if values else None


def billing_summary(db: Session, subscriber: Subscriber) -> dict[str, Any]:
    return {
        "balance": money(_balance_query(db, subscriber.id)),
        "next_bill_date": utc_iso(_next_bill_date_query(db, subscriber.id)),
        "billing_start_date": utc_iso(_billing_start_query(db, subscriber)),
        "invoiced_until": utc_iso(_invoiced_until_query(db, subscriber.id)),
        "blocked_date": utc_iso(_blocked_date_query(db, subscriber.id)),
        "total_paid": money(_total_paid_query(db, subscriber.id)),
    }


def _system_user_name(user: SystemUser) -> str:
    rendered = (
        user.display_name
        or f"{user.first_name or ''} {user.last_name or ''}".strip()
        or user.email
    )
    return rendered or str(user.id)


def _actor_map(db: Session, actor_ids: list[str | None]) -> dict[str, dict[str, Any]]:
    parsed_ids = sorted(
        {
            actor_id
            for actor_id in (_uuid_or_none(value) for value in actor_ids)
            if actor_id is not None
        }
    )
    if not parsed_ids:
        return {}
    rows = list(
        db.scalars(select(SystemUser).where(SystemUser.id.in_(parsed_ids))).all()
    )
    return {
        str(row.id): {
            "id": str(row.id),
            "name": _system_user_name(row),
            "email": row.email,
        }
        for row in rows
    }


def _actor_payload(
    actor_id: str | None, actors: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if not actor_id:
        return None
    resolved = actors.get(str(actor_id))
    if resolved:
        return resolved
    return {"id": str(actor_id), "name": None, "email": None}


def _service_extension_entry_payload(
    entry: ServiceExtensionEntry,
    *,
    subscriber: Subscriber | None = None,
) -> dict[str, Any]:
    return {
        "entry_id": str(entry.id),
        "subscriber_id": str(entry.subscriber_id),
        "customer_id": subscriber.splynx_customer_id if subscriber else None,
        "subscriber_number": subscriber.subscriber_number if subscriber else None,
        "name": subscriber_name(subscriber) if subscriber else None,
        "subscription_id": str(entry.subscription_id),
        "previous_next_billing_at": utc_iso(entry.previous_next_billing_at),
        "new_next_billing_at": utc_iso(entry.new_next_billing_at),
        "created_at": utc_iso(entry.created_at),
    }


def _service_extension_payload(
    extension: ServiceExtension,
    *,
    actors: dict[str, dict[str, Any]] | None = None,
    entries: list[ServiceExtensionEntry] | None = None,
) -> dict[str, Any]:
    actor_lookup = actors or {}
    payload = {
        "id": str(extension.id),
        "reason": extension.reason,
        "window_start": utc_iso(extension.window_start),
        "window_end": utc_iso(extension.window_end),
        "days": extension.days,
        "scope_type": enum_value(extension.scope_type),
        "scope_id": str(extension.scope_id) if extension.scope_id else None,
        "scope_subscriber_ids": extension.scope_subscriber_ids or [],
        "status": enum_value(extension.status),
        "affected_count": extension.affected_count,
        "skipped_count": extension.skipped_count,
        "created_by": _actor_payload(extension.created_by, actor_lookup),
        "applied_by": _actor_payload(extension.applied_by, actor_lookup),
        "applied_at": utc_iso(extension.applied_at),
        "created_at": utc_iso(extension.created_at),
    }
    if entries is not None:
        payload["affected_customers"] = [
            _service_extension_entry_payload(entry) for entry in entries
        ]
    return payload


def service_extension_rows(
    db: Session, *, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    total = db.scalar(select(func.count(ServiceExtension.id))) or 0
    rows = list(
        db.scalars(
            select(ServiceExtension)
            .order_by(
                func.coalesce(
                    ServiceExtension.applied_at,
                    ServiceExtension.created_at,
                ).desc(),
                ServiceExtension.id.desc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    actors = _actor_map(
        db,
        [actor for row in rows for actor in (row.created_by, row.applied_by)],
    )
    return [_service_extension_payload(row, actors=actors) for row in rows], int(total)


def service_extension_detail(db: Session, extension_id: str) -> dict[str, Any] | None:
    parsed = _uuid_or_none(extension_id)
    if parsed is None:
        return None
    extension = db.get(ServiceExtension, parsed)
    if extension is None:
        return None
    entries = list(
        db.scalars(
            select(ServiceExtensionEntry)
            .where(ServiceExtensionEntry.extension_id == extension.id)
            .order_by(ServiceExtensionEntry.created_at.desc())
        ).all()
    )
    subscribers = {
        row.id: row
        for row in db.scalars(
            select(Subscriber).where(
                Subscriber.id.in_([entry.subscriber_id for entry in entries])
            )
        ).all()
    }
    actors = _actor_map(db, [extension.created_by, extension.applied_by])
    payload = _service_extension_payload(extension, actors=actors)
    payload["affected_customers"] = [
        _service_extension_entry_payload(
            entry, subscriber=subscribers.get(entry.subscriber_id)
        )
        for entry in entries
    ]
    return payload


def service_extensions_for_subscriber(
    db: Session, subscriber_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(ServiceExtensionEntry)
            .where(ServiceExtensionEntry.subscriber_id == subscriber_id)
            .options(
                joinedload(ServiceExtensionEntry.extension),
            )
            .order_by(ServiceExtensionEntry.created_at.desc())
        ).all()
    )
    subscriber = db.get(Subscriber, subscriber_id)
    actors = _actor_map(
        db,
        [
            actor
            for row in rows
            for actor in (row.extension.created_by, row.extension.applied_by)
            if row.extension is not None
        ],
    )
    data = []
    for row in rows:
        item = _service_extension_payload(row.extension, actors=actors)
        item["entry"] = _service_extension_entry_payload(row, subscriber=subscriber)
        data.append(item)
    return data


def billing_by_subscriber(
    db: Session, subscribers: list[Subscriber]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not subscribers:
        return {}

    subscriber_ids = [subscriber.id for subscriber in subscribers]
    subscriber_by_id = {subscriber.id: subscriber for subscriber in subscribers}

    balances = {
        account_id: Decimal(str(value or 0))
        for account_id, value in db.execute(
            select(
                Invoice.account_id,
                func.coalesce(func.sum(Invoice.balance_due), 0),
            )
            .where(Invoice.account_id.in_(subscriber_ids))
            .where(Invoice.status.in_(ACTIVE_INVOICE_STATUSES))
            .where(Invoice.is_active.is_(True))
            .group_by(Invoice.account_id)
        ).all()
    }
    invoiced_until = {
        account_id: value
        for account_id, value in db.execute(
            select(Invoice.account_id, func.max(Invoice.billing_period_end))
            .where(Invoice.account_id.in_(subscriber_ids))
            .where(Invoice.is_active.is_(True))
            .group_by(Invoice.account_id)
        ).all()
    }
    total_paid = {
        account_id: Decimal(str(value or 0))
        for account_id, value in db.execute(
            select(Payment.account_id, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.account_id.in_(subscriber_ids))
            .where(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
            .where(Payment.is_active.is_(True))
            .group_by(Payment.account_id)
        ).all()
    }
    next_bill_dates = {
        subscriber_id: value
        for subscriber_id, value in db.execute(
            select(Subscription.subscriber_id, func.min(Subscription.next_billing_at))
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .where(
                Subscription.status.in_(
                    [SubscriptionStatus.active, SubscriptionStatus.pending]
                )
            )
            .group_by(Subscription.subscriber_id)
        ).all()
    }
    subscription_starts = {
        subscriber_id: value
        for subscriber_id, value in db.execute(
            select(Subscription.subscriber_id, func.min(Subscription.start_at))
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .group_by(Subscription.subscriber_id)
        ).all()
    }
    lock_dates = {
        subscriber_id: value
        for subscriber_id, value in db.execute(
            select(EnforcementLock.subscriber_id, func.max(EnforcementLock.created_at))
            .where(EnforcementLock.subscriber_id.in_(subscriber_ids))
            .where(
                EnforcementLock.reason.in_(
                    [EnforcementReason.overdue, EnforcementReason.prepaid]
                )
            )
            .group_by(EnforcementLock.subscriber_id)
        ).all()
    }
    dunning_dates = {
        account_id: value
        for account_id, value in db.execute(
            select(DunningCase.account_id, func.max(DunningCase.started_at))
            .where(DunningCase.account_id.in_(subscriber_ids))
            .group_by(DunningCase.account_id)
        ).all()
    }

    summaries: dict[uuid.UUID, dict[str, Any]] = {}
    for subscriber_id in subscriber_ids:
        subscriber = subscriber_by_id[subscriber_id]
        blocked_values = [
            value
            for value in (
                lock_dates.get(subscriber_id),
                dunning_dates.get(subscriber_id),
            )
            if value is not None
        ]
        billing_start = subscriber.account_start_date or subscription_starts.get(
            subscriber_id
        )
        summaries[subscriber_id] = {
            "balance": money(balances.get(subscriber_id)),
            "next_bill_date": utc_iso(next_bill_dates.get(subscriber_id)),
            "billing_start_date": utc_iso(billing_start),
            "invoiced_until": utc_iso(invoiced_until.get(subscriber_id)),
            "blocked_date": utc_iso(max(blocked_values) if blocked_values else None),
            "total_paid": money(total_paid.get(subscriber_id)),
        }
    return summaries


def latest_payment(db: Session, subscriber_id: uuid.UUID) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.account_id == subscriber_id)
        .filter(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
        .filter(Payment.is_active.is_(True))
        .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
        .first()
    )


def latest_payments_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not subscriber_ids:
        return {}
    ranked = (
        select(
            Payment.account_id.label("account_id"),
            Payment.amount.label("amount"),
            Payment.paid_at.label("paid_at"),
            Payment.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=Payment.account_id,
                order_by=func.coalesce(Payment.paid_at, Payment.created_at).desc(),
            )
            .label("rank"),
        )
        .where(Payment.account_id.in_(subscriber_ids))
        .where(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
        .where(Payment.is_active.is_(True))
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.account_id,
            ranked.c.amount,
            ranked.c.paid_at,
            ranked.c.created_at,
        ).where(ranked.c.rank == 1)
    ).all()
    return {
        account_id: {
            "last_payment_date": utc_iso(paid_at or created_at),
            "last_payment_amount": money(amount),
        }
        for account_id, amount, paid_at, created_at in rows
    }


def _latest_session_by_subscription(
    db: Session, subscription_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RadiusAccountingSession]:
    if not subscription_ids:
        return {}
    rows = list(
        db.scalars(
            select(RadiusAccountingSession)
            .where(RadiusAccountingSession.subscription_id.in_(subscription_ids))
            .order_by(
                RadiusAccountingSession.subscription_id,
                func.coalesce(
                    RadiusAccountingSession.last_update_at,
                    RadiusAccountingSession.session_start,
                    RadiusAccountingSession.created_at,
                ).desc(),
            )
        ).all()
    )
    mapped: dict[uuid.UUID, RadiusAccountingSession] = {}
    for row in rows:
        if row.subscription_id and row.subscription_id not in mapped:
            mapped[row.subscription_id] = row
    return mapped


def latest_session_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RadiusAccountingSession]:
    if not subscriber_ids:
        return {}
    subs = list(
        db.execute(
            select(Subscription.id, Subscription.subscriber_id).where(
                Subscription.subscriber_id.in_(subscriber_ids)
            )
        ).all()
    )
    subscription_ids = [row[0] for row in subs]
    sessions = _latest_session_by_subscription(db, subscription_ids)
    mapped: dict[uuid.UUID, RadiusAccountingSession] = {}
    for subscription_id, subscriber_id in subs:
        session = sessions.get(subscription_id)
        if not session:
            continue
        current = mapped.get(subscriber_id)
        session_seen = (
            session.last_update_at or session.session_start or session.created_at
        )
        current_seen = (
            current.last_update_at or current.session_start or current.created_at
            if current
            else None
        )
        if current is None or (
            session_seen and (current_seen is None or session_seen > current_seen)
        ):
            mapped[subscriber_id] = session
    return mapped


def session_last_seen(session: RadiusAccountingSession | None) -> datetime | None:
    if session is None:
        return None
    return session.last_update_at or session.session_start or session.created_at


def session_state(session: RadiusAccountingSession | None) -> str:
    if session is None:
        return "offline"
    if session.status_type == AccountingStatus.stop or session.session_end is not None:
        return "offline"
    seen = session_last_seen(session)
    if seen is None:
        return "offline"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return "online" if seen >= cutoff_ago(ONLINE_FRESH_SECONDS) else "stale"


def cutoff_ago(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _first_status_event(
    db: Session, subscriber_id: uuid.UUID, statuses: set[SubscriptionStatus]
) -> datetime | None:
    subscription_ids = list(
        db.scalars(
            select(Subscription.id).where(Subscription.subscriber_id == subscriber_id)
        ).all()
    )
    if not subscription_ids:
        return None
    return (
        db.query(func.min(SubscriptionLifecycleEvent.created_at))
        .filter(SubscriptionLifecycleEvent.subscription_id.in_(subscription_ids))
        .filter(SubscriptionLifecycleEvent.to_status.in_(list(statuses)))
        .scalar()
    )


def subscriber_payload(
    db: Session,
    subscriber: Subscriber,
    *,
    services: list[dict[str, Any]] | object = _MISSING,
    billing: dict[str, Any] | object = _MISSING,
    session: RadiusAccountingSession | None = None,
    include_session_state: bool = False,
) -> dict[str, Any]:
    activated_at = (
        subscriber.account_start_date
        or db.query(func.min(Subscription.start_at))
        .filter(Subscription.subscriber_id == subscriber.id)
        .scalar()
        or subscriber.created_at
    )
    suspended_at = _first_status_event(
        db,
        subscriber.id,
        {
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
            SubscriptionStatus.stopped,
        },
    )
    terminated_at = _first_status_event(
        db,
        subscriber.id,
        {
            SubscriptionStatus.disabled,
            SubscriptionStatus.canceled,
            SubscriptionStatus.expired,
            SubscriptionStatus.hidden,
            SubscriptionStatus.archived,
        },
    )
    row: dict[str, Any] = {
        "id": str(subscriber.id),
        "subscriber_number": subscriber.subscriber_number or subscriber.account_number,
        "name": subscriber_name(subscriber),
        "email": subscriber.email,
        "phone": subscriber.phone,
        "status": enum_value(subscriber.status),
        "billing_mode": enum_value(subscriber.billing_mode),
        "billing_day": subscriber.billing_day,
        "address": address_text(subscriber, list(subscriber.addresses or [])),
        "location": location_label(db, subscriber),
        "created_at": utc_iso(subscriber.created_at),
        "activated_at": utc_iso(activated_at),
        "suspended_at": utc_iso(suspended_at),
        "terminated_at": utc_iso(terminated_at),
        "last_seen": utc_iso(session_last_seen(session)),
    }
    if services is not _MISSING:
        row["services"] = services
    if billing is not _MISSING:
        row["billing"] = billing
    if include_session_state:
        row["session_state"] = session_state(session)
    return row


def get_subscriber_or_none(db: Session, subscriber_id: str) -> Subscriber | None:
    parsed = coerce_subscriber_id(subscriber_id)
    if parsed is None:
        return None
    return db.get(Subscriber, parsed)


def list_subscribers(
    db: Session, *, page: int, per_page: int
) -> tuple[list[Subscriber], int]:
    stmt = (
        select(Subscriber)
        .options(selectinload(Subscriber.addresses))
        .order_by(Subscriber.created_at.desc(), Subscriber.id.desc())
    )
    total = db.query(func.count(Subscriber.id)).scalar() or 0
    rows = list(db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all())
    return rows, int(total)


def search_subscribers(
    db: Session, q: str, *, page: int, per_page: int
) -> tuple[list[Subscriber], int]:
    like = f"%{q.strip()}%"
    predicate = or_(
        Subscriber.first_name.ilike(like),
        Subscriber.last_name.ilike(like),
        Subscriber.display_name.ilike(like),
        Subscriber.company_name.ilike(like),
        Subscriber.email.ilike(like),
        Subscriber.subscriber_number.ilike(like),
        Subscriber.account_number.ilike(like),
    )
    total = db.query(func.count(Subscriber.id)).filter(predicate).scalar() or 0
    rows = list(
        db.scalars(
            select(Subscriber)
            .where(predicate)
            .options(selectinload(Subscriber.addresses))
            .order_by(Subscriber.created_at.desc(), Subscriber.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    return rows, int(total)


def locations(db: Session) -> list[dict[str, Any]]:
    result: dict[str, dict[str, str]] = {}
    for pop in db.scalars(
        select(PopSite).where(PopSite.is_active.is_(True)).order_by(PopSite.name)
    ).all():
        result[str(pop.id)] = {"id": str(pop.id), "name": pop.name}
    rows = db.query(Subscriber.city, Subscriber.region).distinct().all()
    for city, region in rows:
        label = city or region
        if not label:
            continue
        key = f"address:{str(label).strip().lower()}"
        result.setdefault(key, {"id": key, "name": str(label)})
    return sorted(result.values(), key=lambda item: item["name"].lower())


def billing_risk_rows(
    db: Session, *, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    subscribers, total = list_subscribers(db, page=page, per_page=per_page)
    subscriber_ids = [item.id for item in subscribers]
    services = services_by_subscriber(db, subscriber_ids)
    billing = billing_by_subscriber(db, subscribers)
    payments = latest_payments_by_subscriber(db, subscriber_ids)
    pop_site_ids = {
        subscriber.pop_site_id for subscriber in subscribers if subscriber.pop_site_id
    }
    pop_names = (
        {
            pop_id: name
            for pop_id, name in db.execute(
                select(PopSite.id, PopSite.name).where(PopSite.id.in_(pop_site_ids))
            ).all()
        }
        if pop_site_ids
        else {}
    )
    rows: list[dict[str, Any]] = []
    for subscriber in subscribers:
        primary_service = next(iter(services.get(subscriber.id, [])), {})
        summary = billing[subscriber.id]
        payment = payments.get(subscriber.id, {})
        location = (
            pop_names.get(subscriber.pop_site_id)
            if subscriber.pop_site_id
            else subscriber.city or subscriber.region or address_text(subscriber)
        )
        rows.append(
            {
                "id": str(subscriber.id),
                "name": subscriber_name(subscriber),
                "email": subscriber.email,
                "phone": subscriber.phone,
                "status": enum_value(subscriber.status),
                "location": location,
                "service_plan": primary_service.get("plan_name"),
                "speed": primary_service.get("speed"),
                "balance": summary["balance"],
                "next_bill_date": summary["next_bill_date"],
                "billing_start_date": summary["billing_start_date"],
                "invoiced_until": summary["invoiced_until"],
                "blocked_date": summary["blocked_date"],
                "total_paid": summary["total_paid"],
                "last_payment_date": payment.get("last_payment_date"),
                "last_payment_amount": payment.get("last_payment_amount", 0.0),
            }
        )
    return rows, total


def online_subscribers(db: Session) -> list[dict[str, Any]]:
    cutoff = cutoff_ago(ONLINE_FRESH_SECONDS)
    rows = list(
        db.query(Subscriber, RadiusAccountingSession)
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .join(
            RadiusAccountingSession,
            RadiusAccountingSession.subscription_id == Subscription.id,
        )
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(
            func.coalesce(
                RadiusAccountingSession.last_update_at,
                RadiusAccountingSession.session_start,
                RadiusAccountingSession.created_at,
            )
            >= cutoff
        )
        .order_by(
            Subscriber.id,
            func.coalesce(
                RadiusAccountingSession.last_update_at,
                RadiusAccountingSession.session_start,
                RadiusAccountingSession.created_at,
            ).desc(),
        )
        .all()
    )
    seen: set[uuid.UUID] = set()
    result: list[dict[str, Any]] = []
    for subscriber, session in rows:
        if subscriber.id in seen:
            continue
        seen.add(subscriber.id)
        result.append(
            {
                "id": str(subscriber.id),
                "subscriber_number": subscriber.subscriber_number
                or subscriber.account_number,
                "status": enum_value(subscriber.status),
                "last_seen": utc_iso(session_last_seen(session)),
            }
        )
    return result


def transaction_rows(
    db: Session,
    *,
    customer_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(InvoiceLine).join(Invoice, InvoiceLine.invoice_id == Invoice.id)
    count_stmt = select(func.count(InvoiceLine.id)).join(
        Invoice, InvoiceLine.invoice_id == Invoice.id
    )
    predicates: list[ColumnElement[bool]] = [
        InvoiceLine.is_active.is_(True),
        Invoice.is_active.is_(True),
    ]
    if customer_id:
        predicates.append(Invoice.account_id == customer_id)
    if date_from:
        predicates.append(
            func.coalesce(Invoice.issued_at, Invoice.created_at) >= date_from
        )
    if date_to:
        predicates.append(
            func.coalesce(Invoice.issued_at, Invoice.created_at) <= date_to
        )
    for predicate in predicates:
        stmt = stmt.where(predicate)
        count_stmt = count_stmt.where(predicate)
    total = db.scalar(count_stmt) or 0
    lines = list(
        db.scalars(
            stmt.options(joinedload(InvoiceLine.invoice))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    rows = []
    for line in lines:
        invoice = line.invoice
        rows.append(
            {
                "id": str(line.id),
                "customer_id": str(invoice.account_id),
                "service_id": str(line.subscription_id)
                if line.subscription_id
                else None,
                "date": utc_iso(invoice.issued_at or invoice.created_at),
                "description": line.description,
                "price": money(line.amount),
                "period_from": utc_iso(invoice.billing_period_start),
                "period_to": utc_iso(invoice.billing_period_end),
            }
        )
    return rows, int(total)


def payment_rows(
    db: Session,
    *,
    customer_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Payment).where(Payment.is_active.is_(True))
    count_stmt = select(func.count(Payment.id)).where(Payment.is_active.is_(True))
    predicates = []
    if customer_id:
        predicates.append(Payment.account_id == customer_id)
    if date_from:
        predicates.append(
            func.coalesce(Payment.paid_at, Payment.created_at) >= date_from
        )
    if date_to:
        predicates.append(func.coalesce(Payment.paid_at, Payment.created_at) <= date_to)
    for predicate in predicates:
        stmt = stmt.where(predicate)
        count_stmt = count_stmt.where(predicate)
    total = db.scalar(count_stmt) or 0
    payments = list(
        db.scalars(
            stmt.options(
                joinedload(Payment.payment_method), joinedload(Payment.payment_channel)
            )
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    rows = []
    for payment in payments:
        method = None
        if payment.payment_channel:
            method = payment.payment_channel.name
        elif payment.payment_method:
            method = enum_value(payment.payment_method.method_type)
        rows.append(
            {
                "id": str(payment.id),
                "customer_id": str(payment.account_id) if payment.account_id else None,
                "amount": money(payment.amount),
                "date": utc_iso(payment.paid_at or payment.created_at),
                "method": method or "unknown",
            }
        )
    return rows, int(total)


def session_rows(db: Session, subscriber_id: uuid.UUID) -> list[dict[str, Any]]:
    subscription_ids = list(
        db.scalars(
            select(Subscription.id).where(Subscription.subscriber_id == subscriber_id)
        ).all()
    )
    if not subscription_ids:
        return []
    sessions = list(
        db.scalars(
            select(RadiusAccountingSession)
            .where(RadiusAccountingSession.subscription_id.in_(subscription_ids))
            .order_by(
                func.coalesce(
                    RadiusAccountingSession.session_start,
                    RadiusAccountingSession.created_at,
                ).desc()
            )
        ).all()
    )
    rows = []
    for session in sessions:
        start = session.session_start or session.created_at
        end = session.session_end
        duration = None
        if start and end:
            duration = max(int((end - start).total_seconds()), 0)
        elif start and session.last_update_at:
            duration = max(int((session.last_update_at - start).total_seconds()), 0)
        rows.append(
            {
                "session_id": session.session_id,
                "start_time": utc_iso(start),
                "end_time": utc_iso(end),
                "duration_seconds": duration,
                "bytes_downloaded": int(session.output_octets or 0),
                "bytes_uploaded": int(session.input_octets or 0),
            }
        )
    return rows


def log_status_writeback(
    db: Session,
    *,
    subscriber_id: uuid.UUID,
    actor: str | None,
    source: str | None,
    reason: str | None,
    requested_status: str | None,
    previous_status: str | None,
    result: str,
    status_code: int,
) -> None:
    db.add(
        AuditEvent(
            actor_type=AuditActorType.service,
            actor_id=actor or "crm",
            action="crm.subscriber_status_writeback",
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            status_code=status_code,
            is_success=status_code < 400,
            metadata_={
                "source": source,
                "reason": reason,
                "requested_status": requested_status,
                "previous_status": previous_status,
                "result": result,
            },
        )
    )


def disable_subscriber_from_crm(
    db: Session,
    subscriber: Subscriber,
    *,
    actor: str | None,
    source: str | None,
    reason: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    previous_status = enum_value(subscriber.status)
    subscriber.status = SubscriberStatus.disabled
    subscriber.is_active = False
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
    )
    for subscription in subscriptions:
        if subscription.status in {
            SubscriptionStatus.blocked,
            SubscriptionStatus.suspended,
            SubscriptionStatus.stopped,
        }:
            old_status = subscription.status
            subscription.status = SubscriptionStatus.disabled
            subscription.end_at = subscription.end_at or now
            subscription.canceled_at = subscription.canceled_at or now
            db.add(
                SubscriptionLifecycleEvent(
                    subscription_id=subscription.id,
                    event_type=LifecycleEventType.cancel,
                    from_status=old_status,
                    to_status=SubscriptionStatus.disabled,
                    reason=reason,
                    actor=actor or source or "crm",
                    metadata_={"source": source or "crm"},
                )
            )
    log_status_writeback(
        db,
        subscriber_id=subscriber.id,
        actor=actor,
        source=source,
        reason=reason,
        requested_status="disabled",
        previous_status=previous_status,
        result="updated",
        status_code=200,
    )
    db.commit()
    db.refresh(subscriber)
    return {"id": str(subscriber.id), "status": enum_value(subscriber.status)}


def create_account_credit(
    db: Session,
    *,
    subscriber_id: str,
    amount: Decimal,
    reason: str | None = None,
    external_ref: str | None = None,
    currency: str = "NGN",
):
    """Issue an account credit (an *issued* credit note) on a subscriber's
    billing account. Used by the CRM to pay out referral rewards.

    Idempotent on ``external_ref`` (embedded in the memo): a repeat call returns
    the existing credit note rather than double-crediting. Raises ``LookupError``
    when the subscriber does not exist.
    """
    from app.models.billing import CreditNote, CreditNoteStatus
    from app.schemas.billing import CreditNoteCreate
    from app.services import billing as billing_service

    sub_uuid = coerce_subscriber_id(str(subscriber_id))
    subscriber = db.get(Subscriber, sub_uuid) if sub_uuid else None
    if subscriber is None or not subscriber.is_active:
        raise LookupError("subscriber_not_found")

    ref_marker = f"[ref:{external_ref}]" if external_ref else ""
    if external_ref:
        existing = (
            db.query(CreditNote)
            .filter(CreditNote.account_id == sub_uuid)
            .filter(CreditNote.is_active.is_(True))
            .filter(CreditNote.memo.ilike(f"%{ref_marker}%"))
            .order_by(CreditNote.created_at.desc())
            .first()
        )
        if existing is not None:
            return existing

    memo = (reason or "Referral reward").strip()
    if ref_marker:
        memo = f"{memo} {ref_marker}"

    payload = CreditNoteCreate(
        account_id=sub_uuid,
        currency=currency,
        subtotal=amount,
        total=amount,
        status=CreditNoteStatus.issued,
        memo=memo,
    )
    return billing_service.credit_notes.create(db, payload)
