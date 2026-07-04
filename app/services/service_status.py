"""Compute a customer's truthful service status.

Service expiry here is NOT date-driven (see unified billing enforcement in
``collections/_core.py:BillingEnforcementReconciler``):

* Prepaid monthly service is invoiced in advance; dunning policy drives cases
  and customer notices, while actual enforcing actions are gated by available
  ledger balance.
* Postpaid never lapses on a date; only dunning on overdue invoices suspends it.

This module mirrors those enforcement rules read-only so the customer app can
show the real "when does my service stop" date instead of guessing from a
billing date.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.schemas.service_status import ServiceStatusItem, ServiceStatusResponse
from app.services import settings_spec
from app.services.collections import get_available_balance, has_overdue_balance
from app.services.common import coerce_uuid

# Statuses the customer still has an operational relationship with (mirrors the
# mobile `currentStatuses`); terminal/historical ones are excluded entirely.
_CURRENT_STATUSES = (
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
    SubscriptionStatus.stopped,
)
_ENDED_STATUSES = frozenset(
    {
        SubscriptionStatus.expired,
        SubscriptionStatus.canceled,
        SubscriptionStatus.disabled,
        SubscriptionStatus.archived,
        SubscriptionStatus.hidden,
    }
)
_NEEDS_PAYMENT_STATUSES = frozenset(
    {SubscriptionStatus.blocked, SubscriptionStatus.suspended}
)


def _prepaid_threshold(db: Session, account: Subscriber) -> Decimal:
    """The min-balance threshold used by the prepaid enforcement gate."""
    if account.min_balance is not None:
        return Decimal(str(account.min_balance))
    default = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_default_min_balance"
    )
    return Decimal(str(default)) if default is not None else Decimal("0.00")


def _grace_days(db: Session, account: Subscriber) -> int:
    if account.grace_period_days is not None:
        return int(account.grace_period_days)
    raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_grace_days"
    )
    try:
        return int(str(raw)) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _overdue_summary(
    db: Session, account_id: str, now: datetime
) -> tuple[Decimal, datetime | None]:
    """Total past-due owed and the oldest overdue due date (mirrors dunning)."""
    rows = (
        db.query(Invoice.balance_due, Invoice.due_at)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.balance_due > 0)
        .filter(
            or_(
                Invoice.status == InvoiceStatus.overdue,
                and_(
                    Invoice.status.in_(
                        [InvoiceStatus.issued, InvoiceStatus.partially_paid]
                    ),
                    Invoice.due_at.is_not(None),
                    Invoice.due_at <= now,
                ),
            )
        )
        .all()
    )
    if not rows:
        return Decimal("0.00"), None
    outstanding = sum((Decimal(str(b or 0)) for b, _ in rows), Decimal("0.00"))
    dues = [d for _, d in rows if d is not None]
    return outstanding, (min(dues) if dues else None)


def build_service_status(db: Session, subscriber_id: str) -> ServiceStatusResponse:
    """Truthful per-account + per-service status for the authenticated caller."""
    now = datetime.now(UTC)
    account = db.get(Subscriber, coerce_uuid(subscriber_id))
    if account is None:
        # Caller authenticated but no subscriber row — empty, not an error.
        return ServiceStatusResponse(as_of=now, billing_mode=BillingMode.prepaid.value)

    # Resolve billing mode through the SAME authority as dunning/enforcement
    # (collectible-subscription-derived, prepaid-wins) so the customer-facing
    # view can never disagree with how the account is actually enforced — e.g.
    # a mixed/drifted account showing a prepaid wallet while dunning treats it
    # as postpaid. Deferred import avoids the service_status <-> collections
    # import cycle. Falls back to the account flag when there are no collectible
    # subscriptions to derive from.
    from app.services.collections._core import _effective_billing_mode_for_account

    account_mode = (
        _effective_billing_mode_for_account(db, account)
        or account.billing_mode
        or BillingMode.prepaid
    )
    resp = ServiceStatusResponse(as_of=now, billing_mode=account_mode.value)

    is_prepaid = account_mode == BillingMode.prepaid
    grace_until: datetime | None = None
    deactivation_at: datetime | None = account.prepaid_deactivation_at

    if is_prepaid:
        balance = get_available_balance(db, subscriber_id)
        threshold = _prepaid_threshold(db, account)
        low = balance < threshold
        resp.balance = balance
        resp.min_balance = threshold
        resp.low_balance = low
        resp.deactivation_at = deactivation_at
        if low:
            grace_days = _grace_days(db, account)
            low_at = account.prepaid_low_balance_at or now
            grace_until = (
                low_at + timedelta(days=grace_days) if grace_days > 0 else low_at
            )
            resp.grace_until = grace_until
    else:
        resp.in_dunning = has_overdue_balance(db, subscriber_id)
        outstanding, oldest_due = _overdue_summary(db, subscriber_id, now)
        resp.outstanding = outstanding
        resp.oldest_overdue_due_at = oldest_due

    subs = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(subscriber_id))
        .filter(Subscription.status.in_(_CURRENT_STATUSES))
        .options(selectinload(Subscription.offer))
        .order_by(Subscription.start_at.desc().nullslast())
        .all()
    )

    for s in subs:
        usable = s.status == SubscriptionStatus.active
        reason = _service_reason(s, is_prepaid, resp)
        expires_at = _service_expires_at(
            s, is_prepaid, resp, grace_until, deactivation_at, usable
        )
        resp.services.append(
            ServiceStatusItem(
                subscription_id=s.id,
                offer_name=s.offer.name if s.offer else None,
                status=s.status.value,
                billing_mode=(s.billing_mode or account_mode).value,
                usable=usable,
                expires_at=expires_at,
                next_charge_at=s.next_billing_at,
                reason=reason,
            )
        )
    return resp


def _service_reason(
    s: Subscription, is_prepaid: bool, resp: ServiceStatusResponse
) -> str:
    if s.status in _NEEDS_PAYMENT_STATUSES:
        return "needs_payment"
    if s.status == SubscriptionStatus.stopped:
        return "stopped"
    if s.status in _ENDED_STATUSES:
        return "ended"
    # active / pending and running:
    if is_prepaid and resp.low_balance:
        return "low_balance"
    if not is_prepaid and resp.in_dunning:
        return "overdue"
    return "ok"


def _service_expires_at(
    s: Subscription,
    is_prepaid: bool,
    resp: ServiceStatusResponse,
    grace_until: datetime | None,
    deactivation_at: datetime | None,
    usable: bool,
) -> datetime | None:
    # An explicit contract end is always the real lapse date.
    if s.end_at is not None:
        return s.end_at
    # A running prepaid service in low-balance will be cut when grace ends (then
    # fully deactivated later) — surface the earliest concrete cut date.
    if is_prepaid and usable and resp.low_balance:
        candidates = [d for d in (grace_until, deactivation_at) if d is not None]
        return min(candidates) if candidates else None
    # Otherwise there is no date-based expiry (postpaid, or healthy prepaid).
    return None
