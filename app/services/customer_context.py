"""Shared customer identity and ownership context.

Customer portal, billing, support, and network views all receive a loose
``customer`` session dict today. This module turns that dict into one
well-defined context so callers stop re-parsing subscriber/account ownership.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import AccountStatus, Subscriber, SubscriberStatus
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid

RESTRICTED_CUSTOMER_STATUSES = frozenset(
    {
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
        SubscriberStatus.disabled,
    }
)


@dataclass(frozen=True)
class CustomerContext:
    """Resolved customer principal, account scope, and portal access state."""

    raw: Mapping[str, object]
    username: str | None
    subscriber_id: str | None
    account_id: str | None
    subscription_id: str | None
    subscriber: Subscriber | None
    account: Subscriber | None
    subscription: Subscription | None
    allowed_account_ids: tuple[str, ...]
    read_only: bool
    is_impersonation: bool

    @property
    def principal_id(self) -> str | None:
        return self.subscriber_id or self.account_id

    @property
    def allowed_subscriber_ids(self) -> tuple[str, ...]:
        return self.allowed_account_ids or (
            (self.subscriber_id,) if self.subscriber_id else ()
        )

    @property
    def is_restricted(self) -> bool:
        return bool(
            self.subscriber is not None
            and self.subscriber.status in RESTRICTED_CUSTOMER_STATUSES
        )

    def owns_account(self, account_id: object | None) -> bool:
        if account_id is None:
            return False
        return str(account_id) in set(self.allowed_account_ids)

    def owns_subscription(self, subscription: Subscription | None) -> bool:
        if subscription is None or subscription.subscriber_id is None:
            return False
        return self.owns_account(subscription.subscriber_id)

    def require_account_id(self) -> str:
        account_id = self.account_id or self.subscriber_id
        if not account_id:
            raise ValueError("Unable to resolve customer account.")
        return account_id


def resolve_customer_context(
    db: Session, customer: Mapping[str, object]
) -> CustomerContext:
    """Resolve a customer session dict into a shared context object."""
    account_id = _clean_id(customer.get("account_id"))
    subscriber_id = _clean_id(
        customer.get("subscriber_id")
        or _nested(customer, "session", "subscriber_id")
        or customer.get("person_id")
    )
    subscription_id = _clean_id(customer.get("subscription_id"))

    subscriber = _get_subscriber(db, subscriber_id)
    account = _get_subscriber(db, account_id)

    if not account_id and not subscription_id and subscriber_id:
        account = _fallback_account_for_subscriber(db, subscriber_id)
        if account is not None:
            account_id = str(account.id)

    subscription = _resolve_subscription(db, subscription_id, account_id)
    if subscription is not None:
        subscription_id = str(subscription.id)
        if not account_id and subscription.subscriber_id:
            account_id = str(subscription.subscriber_id)
            account = _get_subscriber(db, account_id)
        if not subscriber_id and subscription.subscriber_id:
            subscriber_id = str(subscription.subscriber_id)
            subscriber = _get_subscriber(db, subscriber_id)

    if subscriber is None and account is not None:
        subscriber = account
        subscriber_id = str(account.id)
    if account is None and subscriber is not None and account_id == str(subscriber.id):
        account = subscriber

    allowed_account_ids = _allowed_account_ids(
        subscriber=subscriber,
        account_id=account_id,
    )
    return CustomerContext(
        raw=customer,
        username=_clean_text(customer.get("username")),
        subscriber_id=subscriber_id,
        account_id=account_id,
        subscription_id=subscription_id,
        subscriber=subscriber,
        account=account,
        subscription=subscription,
        allowed_account_ids=tuple(allowed_account_ids),
        read_only=bool(customer.get("read_only")),
        is_impersonation=bool(customer.get("is_impersonation")),
    )


def resolve_customer_account_ids(
    db: Session, customer: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """Compatibility shape for older callers: ``(account_id, subscription_id)``."""
    context = resolve_customer_context(db, customer)
    return context.account_id, context.subscription_id


def allowed_customer_account_ids(
    db: Session, customer: Mapping[str, object]
) -> list[str]:
    return list(resolve_customer_context(db, customer).allowed_account_ids)


def allowed_customer_subscriber_ids(
    db: Session, customer: Mapping[str, object]
) -> list[str]:
    return list(resolve_customer_context(db, customer).allowed_subscriber_ids)


def customer_can_access_account(
    db: Session, customer: Mapping[str, object], account_id: object | None
) -> bool:
    """Return whether a customer session may access an account-owned resource."""
    return resolve_customer_context(db, customer).owns_account(account_id)


def require_customer_account_id(db: Session, customer: Mapping[str, object]) -> str:
    """Resolve the customer's primary account ID or raise a user-safe error."""
    return resolve_customer_context(db, customer).require_account_id()


def customer_is_restricted(db: Session, subscriber_id: object) -> bool:
    subscriber = _get_subscriber(db, subscriber_id)
    return bool(
        subscriber is not None and subscriber.status in RESTRICTED_CUSTOMER_STATUSES
    )


def _resolve_subscription(
    db: Session,
    subscription_id: str | None,
    account_id: str | None,
) -> Subscription | None:
    if subscription_id:
        subscription = db.get(Subscription, _safe_uuid(subscription_id))
        if subscription is not None and (
            not account_id or str(subscription.subscriber_id) == str(account_id)
        ):
            return subscription
    if not account_id:
        return None
    account_uuid = _safe_uuid(account_id)
    if account_uuid is None:
        return None
    return (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account_uuid)
        .filter(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.created_at.desc())
        .first()
    )


def _fallback_account_for_subscriber(
    db: Session, subscriber_id: str
) -> Subscriber | None:
    try:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=subscriber_id,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    except Exception:
        return None
    if not accounts:
        return None
    active_account = next(
        (account for account in accounts if account.status == AccountStatus.active),
        None,
    )
    return active_account or accounts[0]


def _allowed_account_ids(
    *,
    subscriber: Subscriber | None,
    account_id: str | None,
) -> list[str]:
    allowed: list[str] = []
    if subscriber is not None:
        allowed.append(str(subscriber.id))
    if account_id and account_id not in allowed:
        allowed.append(account_id)
    return allowed


def _get_subscriber(db: Session, subscriber_id: object | None) -> Subscriber | None:
    coerced = _safe_uuid(subscriber_id)
    if coerced is None:
        return None
    return db.get(Subscriber, coerced)


def _safe_uuid(value: object | None):
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError):
        return None


def _clean_id(value: object | None) -> str | None:
    text = _clean_text(value)
    return text or None


def _clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nested(mapping: Mapping[str, object], key: str, nested_key: str) -> object | None:
    value = mapping.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key)
    return None
