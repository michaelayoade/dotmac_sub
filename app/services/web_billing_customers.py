"""Service helpers for customer/account lookups in billing web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber, SubscriberCategory
from app.services import subscriber as subscriber_service

logger = logging.getLogger(__name__)


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
    if subscriber.category == SubscriberCategory.business:
        return subscriber.company_name or subscriber.display_name or "Subscriber"
    name = " ".join(
        part
        for part in [
            getattr(subscriber, "first_name", ""),
            getattr(subscriber, "last_name", ""),
        ]
        if part
    )
    return name or getattr(subscriber, "display_name", None) or "Subscriber"


def customer_label(db: Session, customer_ref: str | None) -> str | None:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return None
    try:
        if kind == "business":
            subscriber = db.get(Subscriber, ref_id)
            if subscriber and subscriber.category == SubscriberCategory.business:
                return subscriber.company_name or subscriber.display_name or subscriber.full_name
        else:
            subscriber = db.get(Subscriber, ref_id)
            if subscriber:
                return subscriber_label(subscriber)
    except Exception:
        return None
    return None


def subscriber_ids_for_customer(db: Session, customer_ref: str | None) -> list[str]:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return []
    if kind == "business":
        subscriber = db.get(Subscriber, ref_id)
        if subscriber and subscriber.category == SubscriberCategory.business:
            return [str(subscriber.id)]
        return []
    subscriber = db.get(Subscriber, ref_id)
    if subscriber:
        return [str(subscriber.id)]
    return []


def accounts_for_customer(db: Session, customer_ref: str | None) -> list[dict]:
    kind, ref_id = parse_customer_ref(customer_ref)
    if not ref_id:
        return []
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
    if account.category == SubscriberCategory.business:
        name = account.company_name or account.display_name or ""
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
