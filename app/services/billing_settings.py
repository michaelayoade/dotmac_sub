"""Helpers for resolving billing settings with legacy fallbacks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain

# A subscription in one of these states represents a *live* (connectable)
# service. Used for "is the service actually up" semantics.
LIVE_SERVICE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.suspended,
    SubscriptionStatus.pending,
)

# Statuses we still actively COLLECT against (invoice reminders, dunning
# escalations, autopay charges). Deliberately wider than
# ``LIVE_SERVICE_STATUSES``: it adds ``blocked``, which is a *recoverable
# non-payment hold*, not a dead account — exactly the customer we most want to
# keep chasing and auto-charging so they can pay and be restored. Excluding it
# (the pre-2026-06-26 behavior) meant that the moment enforcement walled a
# non-payer, autopay/reminders/dunning could never recover them — a major
# collections leak. Paused states (``stopped`` and the reversible ``disabled``)
# and terminal states (``hidden``, ``archived``, ``canceled`` and ``expired``)
# stay excluded — these must not keep pinging or charging the customer.
COLLECTIBLE_SERVICE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.suspended,
    SubscriptionStatus.pending,
    SubscriptionStatus.blocked,
)


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _domain_setting_value(
    db: Session, domain: SettingDomain, key: str
) -> object | None:
    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == domain)
        .where(DomainSetting.key == key)
        .where(DomainSetting.is_active.is_(True))
    )
    setting = db.scalars(stmt).first()
    if not setting:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def _setting_value(db: Session, key: str) -> object | None:
    return _domain_setting_value(db, SettingDomain.billing, key)


def resolve_payment_due_days(
    db: Session,
    default: int = 14,
    subscriber: object | None = None,
) -> int:
    """Resolve payment due days: subscriber override > global setting > legacy keys.

    Args:
        db: Database session.
        default: Fallback if no setting is found.
        subscriber: Optional subscriber — if they have ``payment_due_days``
            set, that value takes priority over the global setting.
    """
    # Subscriber-level override takes priority
    sub_due_days = getattr(subscriber, "payment_due_days", None)
    if sub_due_days is not None:
        return max(_coerce_int(sub_due_days, default), 0)

    canonical = _setting_value(db, "payment_due_days")
    if canonical is not None:
        return max(_coerce_int(canonical, default), 0)

    legacy_invoice = _setting_value(db, "invoice_due_days")
    if legacy_invoice is not None:
        return max(_coerce_int(legacy_invoice, default), 0)

    legacy_terms = _setting_value(db, "default_payment_terms_days")
    if legacy_terms is not None:
        return max(_coerce_int(legacy_terms, default), 0)

    return default


def accounts_with_live_service(db: Session) -> set:
    """Subscriber IDs that have at least one subscription in a live service
    state (see :data:`LIVE_SERVICE_STATUSES`).

    Billing automation that *chases an existing balance* — invoice reminders,
    dunning escalations, autopay charges — must skip accounts whose services
    are all paused or terminal: a disabled/canceled/expired service should not
    keep pinging or charging the customer. This mirrors the eligibility gate in
    ``collections.DunningWorkflow`` but spans every billing mode, since
    reminders and autopay are not postpaid-specific.
    """
    return set(
        db.scalars(
            select(Subscription.subscriber_id)
            .where(Subscription.status.in_(LIVE_SERVICE_STATUSES))
            .distinct()
        ).all()
    )


def account_has_live_service(db: Session, account_id) -> bool:
    """Whether a single account still has a live (billable) service.

    Single-account counterpart to :func:`accounts_with_live_service`, for hot
    paths that already operate on one account (e.g. autopay) and only need a
    cheap existence check rather than the full set.
    """
    return (
        db.scalars(
            select(Subscription.id)
            .where(Subscription.subscriber_id == account_id)
            .where(Subscription.status.in_(LIVE_SERVICE_STATUSES))
            .limit(1)
        ).first()
        is not None
    )


def accounts_with_collectible_service(db: Session) -> set:
    """Subscriber IDs with at least one subscription we still collect against
    (see :data:`COLLECTIBLE_SERVICE_STATUSES`).

    This is the gate collections automation should use — invoice reminders,
    dunning escalations, autopay charges. Unlike
    :func:`accounts_with_live_service`, it keeps ``blocked`` (recoverable
    non-payment) in scope so a walled non-payer can still be reminded and
    auto-charged back to good standing.
    """
    return set(
        db.scalars(
            select(Subscription.subscriber_id)
            .where(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
            .distinct()
        ).all()
    )


def account_has_collectible_service(db: Session, account_id) -> bool:
    """Whether a single account still has a collectible service.

    Single-account counterpart to :func:`accounts_with_collectible_service`,
    for hot paths like autopay that operate on one account.
    """
    return (
        db.scalars(
            select(Subscription.id)
            .where(Subscription.subscriber_id == account_id)
            .where(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
            .limit(1)
        ).first()
        is not None
    )
