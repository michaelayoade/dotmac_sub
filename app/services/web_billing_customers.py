"""Service helpers for customer/account lookups in billing web routes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.subscriber import Organization, Subscriber
from app.services import subscriber as subscriber_service


def parse_customer_ref(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    raw = value.strip()
    if ":" in raw:
        kind, ref_id = raw.split(":", 1)
        return kind, ref_id
    return None, raw


def subscriber_label(subscriber: Subscriber | None) -> str:
    if not subscriber:
        return "Subscriber"
    if getattr(subscriber, "organization", None):
        return subscriber.organization.name or "Subscriber"
    name = " ".join(
        part for part in [getattr(subscriber, "first_name", ""), getattr(subscriber, "last_name", "")] if part
    )
    return name or getattr(subscriber, "display_name", None) or "Subscriber"


def customer_label(db: Session, customer_ref: str | None) -> str | None:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return None
    try:
        if kind == "organization":
            organization = db.get(Organization, ref_id)
            if organization:
                return organization.name
        else:
            subscriber = db.get(Subscriber, ref_id)
            if subscriber:
                label = " ".join(part for part in [subscriber.first_name, subscriber.last_name] if part)
                return label or None
    except Exception:
        return None
    return None


def subscriber_ids_for_customer(db: Session, customer_ref: str | None) -> list[str]:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    if kind == "organization":
        subscribers = subscriber_service.subscribers.list(
            db=db,
            organization_id=ref_id,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        return [str(sub.id) for sub in subscribers or []]
    subscriber = db.get(Subscriber, ref_id)
    if subscriber:
        return [str(subscriber.id)]
    return []


def accounts_for_customer(db: Session, customer_ref: str | None) -> list[dict]:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    subscribers = []
    if kind == "organization":
        subscribers = subscriber_service.subscribers.list(
            db=db,
            organization_id=ref_id,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    else:
        subscriber = db.get(Subscriber, ref_id)
        subscribers = [subscriber] if subscriber else []
    return [
        {
            "id": str(subscriber.id),
            "label": account_label(subscriber),
            "account_number": subscriber.account_number,
        }
        for subscriber in subscribers or []
    ]


def subscribers_for_customer(db: Session, customer_ref: str | None) -> list[dict]:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    subscribers = []
    if kind == "organization":
        subscribers = subscriber_service.subscribers.list(
            db=db,
            organization_id=ref_id,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    else:
        subscriber = db.get(Subscriber, ref_id)
        subscribers = [subscriber] if subscriber else []
    return [
        {
            "id": str(sub.id),
            "label": subscriber_label(sub),
        }
        for sub in subscribers or []
    ]


def account_label(account: Subscriber | None) -> str:
    if not account:
        return "Account"
    if getattr(account, "organization", None):
        name = account.organization.name or ""
        if name:
            return name
    label = f"{getattr(account, 'first_name', '')} {getattr(account, 'last_name', '')}".strip()
    if label:
        return label
    display_name = getattr(account, "display_name", None)
    if isinstance(display_name, str) and display_name:
        return display_name
    if getattr(account, "account_number", None):
        return f"Account {account.account_number}"
    return "Account"
