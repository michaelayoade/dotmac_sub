"""Helpers for admin subscriber detail page."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import CreditNoteStatus, InvoiceStatus
from app.models.catalog import ContractTerm, OfferStatus, SubscriptionStatus
from app.models.network import FdhCabinet, FiberSpliceClosure
from app.models.subscriber import Address, Subscriber
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services.audit_helpers import extract_changes, format_changes


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two lat/lon points in meters."""
    radius_m = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _resolve_subscriber_addresses(db: Session, subscriber_id):
    addresses = db.query(Address).filter(Address.subscriber_id == subscriber_id).all()
    primary_address = next(
        (addr for addr in addresses if addr.is_primary),
        addresses[0] if addresses else None,
    )
    return addresses, primary_address


def build_subscriber_map_data(db: Session, subscriber, primary_address):
    """Build mini-map GeoJSON payload for subscriber detail page."""
    if not (
        primary_address
        and primary_address.latitude is not None
        and primary_address.longitude is not None
    ):
        return None
    customer_lat = float(primary_address.latitude)
    customer_lon = float(primary_address.longitude)

    customer_name = "Customer"
    if getattr(subscriber, "organization", None):
        customer_name = subscriber.organization.name or "Customer"
    else:
        full_name = f"{getattr(subscriber, 'first_name', '')} {getattr(subscriber, 'last_name', '')}".strip()
        customer_name = full_name or getattr(subscriber, "display_name", None) or "Customer"

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [primary_address.longitude, primary_address.latitude],
            },
            "properties": {
                "type": "customer",
                "name": customer_name,
                "address": primary_address.address_line1,
            },
        }
    ]

    fdh_cabinets = (
        db.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .filter(FdhCabinet.latitude.isnot(None))
        .filter(FdhCabinet.longitude.isnot(None))
        .all()
    )
    for fdh in fdh_cabinets:
        if fdh.latitude is None or fdh.longitude is None:
            continue
        distance = _haversine_distance(
            customer_lat,
            customer_lon,
            float(fdh.latitude),
            float(fdh.longitude),
        )
        if distance <= 2000:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
                    "properties": {
                        "type": "fdh_cabinet",
                        "name": fdh.name,
                        "code": fdh.code,
                        "distance_m": round(distance),
                    },
                }
            )

    closures = (
        db.query(FiberSpliceClosure)
        .filter(FiberSpliceClosure.is_active.is_(True))
        .filter(FiberSpliceClosure.latitude.isnot(None))
        .filter(FiberSpliceClosure.longitude.isnot(None))
        .all()
    )
    for closure in closures:
        if closure.latitude is None or closure.longitude is None:
            continue
        distance = _haversine_distance(
            customer_lat,
            customer_lon,
            float(closure.latitude),
            float(closure.longitude),
        )
        if distance <= 1000:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [closure.longitude, closure.latitude],
                    },
                    "properties": {
                        "type": "splice_closure",
                        "name": closure.name,
                        "distance_m": round(distance),
                    },
                }
            )

    return {
        "center": [primary_address.latitude, primary_address.longitude],
        "geojson": {"type": "FeatureCollection", "features": features},
    }


def build_subscriber_geocode_target(primary_address):
    """Build geocode payload when address exists without coordinates."""
    if not primary_address or not (primary_address.address_line1 or "").strip():
        return None
    if (
        getattr(primary_address, "latitude", None) is not None
        and getattr(primary_address, "longitude", None) is not None
    ):
        return None
    target_id = getattr(primary_address, "id", None)
    return {
        "id": str(target_id) if target_id is not None else None,
        "address_line1": primary_address.address_line1,
        "address_line2": primary_address.address_line2,
        "city": primary_address.city,
        "region": primary_address.region,
        "postal_code": primary_address.postal_code,
        "country_code": primary_address.country_code,
        "payload": {
            "address_line1": primary_address.address_line1,
            "address_line2": primary_address.address_line2 or "",
            "city": primary_address.city or "",
            "region": primary_address.region or "",
            "postal_code": primary_address.postal_code or "",
            "country_code": primary_address.country_code or "",
        },
    }


def build_subscriber_detail_snapshot(db: Session, subscriber, subscriber_id):
    """Collect subscriber detail page data from multiple services."""
    subscriptions = []
    online_status = {}
    try:
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(subscriber.id),
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        subscriptions = sorted(
            subscriptions,
            key=lambda item: item.created_at or item.id,
            reverse=True,
        )[:10]
        for sub in subscriptions:
            latest_session = (
                db.query(RadiusAccountingSession)
                .filter(RadiusAccountingSession.subscription_id == sub.id)
                .order_by(RadiusAccountingSession.created_at.desc())
                .first()
            )
            if latest_session:
                online_status[str(sub.id)] = (
                    latest_session.status_type
                    in (AccountingStatus.start, AccountingStatus.interim)
                    and latest_session.session_end is None
                )
            else:
                online_status[str(sub.id)] = False
    except Exception:
        subscriptions = []
        online_status = {}

    accounts = []
    invoices = []
    balance_due = Decimal("0.00")
    available_credit = Decimal("0.00")
    current_balance = Decimal("0.00")
    try:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=str(subscriber_id),
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=25,
            offset=0,
        )
        if accounts:
            account = accounts[0]
            invoices = billing_service.invoices.list(
                db=db,
                account_id=account.id,
                status=None,
                is_active=None,
                order_by="created_at",
                order_dir="desc",
                limit=5,
                offset=0,
            )
            balance_due = sum(
                Decimal(str(getattr(inv, "balance_due", 0) or 0))
                for inv in invoices
                if inv.status
                in (InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue)
            )
            credit_notes = billing_service.credit_notes.list(
                db=db,
                account_id=account.id,
                invoice_id=None,
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=100,
                offset=0,
            )
            available_credit = sum(
                Decimal(str(note.total or 0)) - Decimal(str(note.applied_total or 0))
                for note in credit_notes
                if note.status in (CreditNoteStatus.issued, CreditNoteStatus.partially_applied)
            )
            current_balance = balance_due + available_credit
    except Exception:
        accounts = []
        invoices = []
        balance_due = Decimal("0.00")
        available_credit = Decimal("0.00")
        current_balance = Decimal("0.00")

    notifications = []
    try:
        recipients = []
        if getattr(subscriber, "email", None):
            recipients.append(subscriber.email)
        if getattr(subscriber, "phone", None):
            recipients.append(subscriber.phone)
        if recipients:
            all_notifications = notification_service.Notifications.list(
                db=db,
                channel=None,
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=100,
                offset=0,
            )
            notifications = [
                item for item in all_notifications if item.recipient in recipients
            ][:5]
    except Exception:
        notifications = []

    monthly_bill = (
        sum(float(getattr(sub, "price", 0) or 0) for sub in subscriptions)
        if subscriptions
        else 0
    )
    stats = {
        "monthly_bill": monthly_bill,
        "balance_due": float(balance_due),
        "credit_issued": float(available_credit),
        "current_balance": float(current_balance),
        "has_credit_adjustment": available_credit > 0,
        "data_usage": "0",
    }

    addresses, primary_address = _resolve_subscriber_addresses(db, subscriber_id)
    map_data = build_subscriber_map_data(db, subscriber, primary_address)
    geocode_target = build_subscriber_geocode_target(primary_address)

    return {
        "accounts": accounts,
        "subscriptions": subscriptions,
        "online_status": online_status,
        "invoices": invoices,
        "notifications": notifications,
        "stats": stats,
        "addresses": addresses,
        "primary_address": primary_address,
        "map_data": map_data,
        "geocode_target": geocode_target,
    }


def build_subscriber_timeline(db: Session, subscriber_id):
    """Build audit timeline for subscriber detail page."""
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {
        str(event.actor_id)
        for event in audit_events
        if getattr(event, "actor_id", None)
    }
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    timeline = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        comment_text = str(metadata.get("comment") or "").strip()
        is_todo = bool(metadata.get("is_todo"))
        is_completed = bool(metadata.get("is_completed"))
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        if comment_text:
            detail = f"{actor_name} · {comment_text}"
        else:
            detail = actor_name if not change_summary else f"{actor_name} · {change_summary}"
        timeline.append(
            {
                "id": str(event.id),
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "detail": detail,
                "is_todo": is_todo,
                "is_completed": is_completed,
                "time": (
                    event.occurred_at.strftime("%b %d, %Y %H:%M")
                    if event.occurred_at
                    else "Just now"
                ),
            }
        )
    return timeline


def build_subscriber_detail_page_context(db: Session, subscriber_id):
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found")

    detail_snapshot = build_subscriber_detail_snapshot(
        db=db,
        subscriber=subscriber,
        subscriber_id=subscriber_id,
    )
    timeline = build_subscriber_timeline(db=db, subscriber_id=subscriber_id)
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=OfferStatus.active.value,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    return {
        "subscriber": subscriber,
        **detail_snapshot,
        "billing_config": _build_billing_config(subscriber, detail_snapshot.get("stats") or {}),
        "timeline": timeline,
        "offers": offers,
        "subscription_statuses": [s.value for s in SubscriptionStatus],
        "contract_terms": [t.value for t in ContractTerm],
    }


def _build_billing_config(subscriber, stats: dict) -> dict[str, object]:
    metadata = dict(getattr(subscriber, "metadata_", None) or {})
    blocking_days = int(metadata.get("blocking_period_days") or 0)
    deactivation_days = int(metadata.get("deactivation_period_days") or 0)
    auto_create = bool(metadata.get("auto_create_invoices", True))
    send_notifications = bool(metadata.get("send_billing_notifications", True))

    next_block_at = None
    next_block_label = "No block scheduled"
    balance_due = float(stats.get("balance_due") or 0)
    if balance_due > 0:
        delay_days = max(blocking_days, int(getattr(subscriber, "grace_period_days", 0) or 0))
        next_block_at = datetime.now(UTC) + timedelta(days=delay_days)
        if delay_days <= 0:
            next_block_label = "Immediately"
        elif delay_days <= 30:
            next_block_label = f"In {delay_days} day(s)"
        else:
            next_block_label = next_block_at.strftime("%Y-%m-%d")

    return {
        "category": getattr(subscriber, "category", None),
        "billing_day": getattr(subscriber, "billing_day", None),
        "payment_due_days": getattr(subscriber, "payment_due_days", None),
        "grace_period_days": getattr(subscriber, "grace_period_days", None),
        "min_balance": getattr(subscriber, "min_balance", None),
        "billing_enabled": bool(getattr(subscriber, "billing_enabled", True)),
        "blocking_period_days": blocking_days,
        "deactivation_period_days": deactivation_days,
        "auto_create_invoices": auto_create,
        "send_billing_notifications": send_notifications,
        "next_block_at": next_block_at,
        "next_block_label": next_block_label,
    }
