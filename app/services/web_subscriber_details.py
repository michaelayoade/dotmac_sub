"""Helpers for admin subscriber detail page."""

import math

from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus
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
    balance_due = 0.0
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
                float(getattr(inv, "total_amount", 0) or 0)
                for inv in invoices
                if inv.status in (InvoiceStatus.issued, InvoiceStatus.overdue)
            )
    except Exception:
        accounts = []
        invoices = []
        balance_due = 0.0

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
        "balance_due": balance_due,
        "data_usage": "0",
    }

    addresses, primary_address = _resolve_subscriber_addresses(db, subscriber_id)
    map_data = build_subscriber_map_data(db, subscriber, primary_address)

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
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        detail = actor_name if not change_summary else f"{actor_name} · {change_summary}"
        timeline.append(
            {
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "detail": detail,
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
        "timeline": timeline,
        "offers": offers,
        "subscription_statuses": [s.value for s in SubscriptionStatus],
        "contract_terms": [t.value for t in ContractTerm],
    }
