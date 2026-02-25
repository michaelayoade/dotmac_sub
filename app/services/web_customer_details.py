"""Service helpers for web/admin customer detail pages."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.catalog import SubscriptionStatus
from app.models.subscriber import (
    ChannelType,
    Subscriber,
    SubscriberChannel,
)
from app.schemas.geocoding import GeocodePreviewRequest
from app.services import audit as audit_service
from app.services import catalog as catalog_service
from app.services import geocoding as geocoding_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services.audit_helpers import extract_changes, format_changes


def _dedupe_accounts(accounts):
    unique = {}
    for account in accounts:
        unique[str(account.id)] = account
    return list(unique.values())


def _list_subscriptions_for_accounts(db: Session, accounts):
    if not accounts:
        return []
    subscriptions = []
    for account in accounts:
        try:
            account_subs = catalog_service.subscriptions.list(
                db=db,
                subscriber_id=str(account.id),
                offer_id=None,
                status=None,
                order_by="created_at",
                order_dir="desc",
                limit=200,
                offset=0,
            )
            subscriptions.extend(account_subs)
        except Exception:
            continue
    return subscriptions


def _format_contact_channel(subscriber: Subscriber, channel: SubscriberChannel) -> dict[str, object]:
    return {
        "id": str(channel.id),
        "first_name": subscriber.first_name or "",
        "last_name": subscriber.last_name or "",
        "role": None,
        "title": channel.label,
        "is_primary": channel.is_primary,
        "email": channel.address if channel.channel_type == ChannelType.email else "",
        "phone": channel.address if channel.channel_type == ChannelType.phone else "",
    }


def _build_activity_items(db: Session, entity_type: str, entity_id: str):
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in audit_events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activity_items = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        description_parts = [actor_name]
        if change_summary:
            description_parts.append(change_summary)
        activity_items.append(
            {
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": " · ".join(description_parts),
                "timestamp": event.occurred_at,
            }
        )
    return activity_items


def _build_common_financials(db: Session, account_ids):
    invoices = []
    payments = []
    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(Invoice.created_at.desc())
            .limit(10)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .order_by(Payment.created_at.desc())
            .limit(10)
            .all()
        )

    balance_due = sum(
        float(getattr(inv, "balance_due", 0) or 0)
        for inv in invoices
        if inv.status in (InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue)
    )

    total_invoiced = 0
    total_paid = 0
    overdue_invoices = 0
    last_payment = None
    last_invoice = None
    if account_ids:
        total_invoiced = (
            db.query(func.coalesce(func.sum(Invoice.total), 0))
            .filter(Invoice.account_id.in_(account_ids))
            .scalar()
            or 0
        )
        total_paid = (
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .scalar()
            or 0
        )
        overdue_invoices = (
            db.query(func.count(Invoice.id))
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.status == InvoiceStatus.overdue)
            .scalar()
            or 0
        )
        last_payment = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .first()
        )
        last_invoice = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .first()
        )

    return {
        "invoices": invoices,
        "payments": payments,
        "balance_due": balance_due,
        "financials": {
            "total_invoiced": total_invoiced,
            "total_paid": total_paid,
            "overdue_invoices": overdue_invoices,
            "last_payment": last_payment,
            "last_invoice": last_invoice,
        },
    }


def _build_person_fallback_address(db: Session, customer: Subscriber):
    if (customer.address_line1 or "").strip() == "":
        return []

    customer_meta = getattr(customer, "metadata_", None) or {}
    customer_lat = getattr(customer, "latitude", None)
    customer_lng = getattr(customer, "longitude", None)

    def _clean_value(value):
        if isinstance(value, str):
            trimmed = value.strip()
            return None if not trimmed or trimmed.lower() == "none" else trimmed
        return value

    address_line1 = (customer.address_line1 or "").strip()
    address_line2 = _clean_value(getattr(customer, "address_line2", None))
    city = _clean_value(getattr(customer, "city", None))
    region = _clean_value(getattr(customer, "region", None))
    postal_code = _clean_value(getattr(customer, "postal_code", None))
    country_code = _clean_value(getattr(customer, "country_code", None))
    if customer_lat is None:
        customer_lat = customer_meta.get("latitude")
    if customer_lng is None:
        customer_lng = customer_meta.get("longitude")

    if customer_lat is None or customer_lng is None:
        try:
            payload = GeocodePreviewRequest(
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                region=region,
                postal_code=postal_code,
                country_code=country_code,
                limit=1,
            )
            results = geocoding_service.geocode_preview_from_request(db, payload)
            if results:
                first = results[0] or {}
                lat_value = first.get("latitude")
                lng_value = first.get("longitude")
                if lat_value is not None and lng_value is not None:
                    customer_lat = float(lat_value)
                    customer_lng = float(lng_value)
                    if getattr(customer, "metadata_", None) is None:
                        customer.metadata_ = {}
                    if isinstance(customer.metadata_, dict):
                        customer.metadata_["latitude"] = customer_lat
                        customer.metadata_["longitude"] = customer_lng
                        try:
                            db.add(customer)
                            db.commit()
                        except Exception:
                            db.rollback()
        except Exception:
            pass

    return [
        SimpleNamespace(
            id=None,
            is_primary=True,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            latitude=customer_lat,
            longitude=customer_lng,
            created_at=None,
        )
    ]


def _build_map_payload(primary_address, customer_name: str):
    map_data = None
    geocode_target = None
    if primary_address and (primary_address.address_line1 or "").strip():
        if (
            getattr(primary_address, "latitude", None) is not None
            and getattr(primary_address, "longitude", None) is not None
        ):
            map_data = {
                "center": [primary_address.latitude, primary_address.longitude],
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
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
                    ],
                },
            }
        else:
            target_id = getattr(primary_address, "id", None)
            geocode_target = {
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
    return map_data, geocode_target


def build_person_detail_snapshot(db: Session, customer_id: str):
    customer = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    subscribers = [customer]
    addresses = []
    contacts: list[dict[str, object]] = []
    accounts = []
    for sub in subscribers:
        try:
            sub_addresses = subscriber_service.addresses.list(
                db=db,
                subscriber_id=str(sub.id),
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            addresses.extend(sub_addresses)
        except Exception:
            pass
        accounts.append(sub)

    accounts = _dedupe_accounts(accounts)
    subscriptions = _list_subscriptions_for_accounts(db, accounts)
    account_lookup = {str(account.id): account for account in accounts}
    account_ids = [account.id for account in accounts]

    finance_data = _build_common_financials(db, account_ids)
    invoices = finance_data["invoices"]
    payments = finance_data["payments"]
    balance_due = finance_data["balance_due"]
    financials = finance_data["financials"]
    active_subscriptions = sum(1 for sub in subscriptions if sub.status == SubscriptionStatus.active)
    monthly_recurring = sum(
        float(getattr(sub, "unit_price", 0) or 0)
        for sub in subscriptions
        if sub.status == SubscriptionStatus.active
    )
    financials["monthly_recurring"] = monthly_recurring

    if not addresses:
        addresses = _build_person_fallback_address(db, customer)

    primary_address = next(
        (a for a in addresses if getattr(a, "is_primary", False) and (a.address_line1 or "").strip()),
        next(
            (a for a in addresses if getattr(a, "is_primary", False)),
            next((a for a in addresses if (a.address_line1 or "").strip()), addresses[0] if addresses else None),
        ),
    )
    map_data, geocode_target = _build_map_payload(
        primary_address,
        f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
    )

    active_subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.id == customer.id)
        .filter(Subscriber.is_active.is_(True))
        .count()
    )
    total_subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.id == customer.id)
        .count()
    )

    notifications = []
    try:
        recipients = []
        if customer.email:
            recipients.append(customer.email)
        if customer.phone:
            recipients.append(customer.phone)
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
            notifications = [n for n in all_notifications if n.recipient in recipients][:5]
    except Exception:
        notifications = []

    activity_items = _build_activity_items(db, "subscriber", str(customer_id))
    stats = {
        "total_subscribers": len(subscribers),
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": active_subscriptions,
        "balance_due": balance_due,
        "total_addresses": len(addresses),
        "total_contacts": len(contacts),
    }

    return {
        "customer": customer,
        "customer_type": "person",
        "customer_name": f"{customer.first_name} {customer.last_name}",
        "subscribers": subscribers,
        "accounts": accounts,
        "subscriptions": subscriptions,
        "account_lookup": account_lookup,
        "addresses": addresses,
        "primary_address": primary_address,
        "map_data": map_data,
        "geocode_target": geocode_target,
        "contacts": contacts,
        "invoices": invoices,
        "payments": payments,
        "notifications": notifications,
        "stats": stats,
        "financials": financials,
        "has_active_subscribers": active_subscribers > 0,
        "has_any_subscribers": total_subscribers > 0,
        "activity_items": activity_items,
    }


def build_organization_detail_snapshot(db: Session, customer_id: str):
    customer = subscriber_service.organizations.get(db=db, organization_id=customer_id)
    org_uuid = UUID(customer_id)
    subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.organization_id == org_uuid)
        .order_by(Subscriber.created_at.desc())
        .limit(10)
        .all()
    )
    addresses = []
    contacts: list[dict[str, object]] = []
    accounts = []
    for sub in subscribers:
        try:
            sub_addresses = subscriber_service.addresses.list(
                db=db,
                subscriber_id=str(sub.id),
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            addresses.extend(sub_addresses)
        except Exception:
            pass
        accounts.append(sub)
        channels = (
            db.query(SubscriberChannel)
            .filter(SubscriberChannel.subscriber_id == sub.id)
            .order_by(SubscriberChannel.created_at.desc())
            .limit(50)
            .all()
        )
        contacts.extend(_format_contact_channel(sub, channel) for channel in channels)

    accounts = _dedupe_accounts(accounts)
    subscriptions = _list_subscriptions_for_accounts(db, accounts)
    account_lookup = {str(account.id): account for account in accounts}
    account_ids = [account.id for account in accounts]

    finance_data = _build_common_financials(db, account_ids)
    invoices = finance_data["invoices"]
    payments = finance_data["payments"]
    balance_due = finance_data["balance_due"]
    financials = finance_data["financials"]
    active_subscriptions = sum(1 for sub in subscriptions if sub.status == SubscriptionStatus.active)
    monthly_recurring = sum(
        float(getattr(sub, "unit_price", 0) or 0)
        for sub in subscriptions
        if sub.status == SubscriptionStatus.active
    )
    financials["monthly_recurring"] = monthly_recurring

    primary_address = next(
        (a for a in addresses if getattr(a, "is_primary", False)),
        addresses[0] if addresses else None,
    )
    map_data, geocode_target = _build_map_payload(primary_address, customer.name)

    active_subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.organization_id == org_uuid)
        .filter(Subscriber.is_active.is_(True))
        .count()
    )
    total_subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.organization_id == org_uuid)
        .count()
    )

    notifications = []
    try:
        recipients = []
        org_people = db.query(Subscriber).filter(Subscriber.organization_id == org_uuid).limit(10).all()
        for person in org_people:
            if person.email:
                recipients.append(person.email)
            if person.phone:
                recipients.append(person.phone)
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
            notifications = [n for n in all_notifications if n.recipient in recipients][:5]
    except Exception:
        notifications = []

    activity_items = _build_activity_items(db, "organization", str(customer_id))
    stats = {
        "total_subscribers": len(subscribers),
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": active_subscriptions,
        "balance_due": balance_due,
        "total_addresses": len(addresses),
        "total_contacts": len(contacts),
    }

    return {
        "customer": customer,
        "customer_type": "organization",
        "customer_name": customer.name,
        "subscribers": subscribers,
        "accounts": accounts,
        "subscriptions": subscriptions,
        "account_lookup": account_lookup,
        "addresses": addresses,
        "primary_address": primary_address,
        "map_data": map_data,
        "geocode_target": geocode_target,
        "contacts": contacts,
        "invoices": invoices,
        "payments": payments,
        "notifications": notifications,
        "stats": stats,
        "financials": financials,
        "has_active_subscribers": active_subscribers > 0,
        "has_any_subscribers": total_subscribers > 0,
        "activity_items": activity_items,
    }
