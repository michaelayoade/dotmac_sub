"""Prepaid drawdown engine: periodic charges that decrement a prepaid balance.

Splynx's prepaid customers (``prepaid_monthly`` / ``prepaid``) pay in advance:
a periodic charge draws their balance down, and service is suspended when the
balance falls below ``min_balance``. DotMac previously had no mechanism to
*decrement* the balance, so a cut-over prepaid customer would get free service.

This posts the periodic charge as a debit ``LedgerEntry`` (the same primitive
used by prepaid plan-change and add-on charges). The existing prepaid
enforcement (``collections/_core.py``) reads the resulting balance and handles
warn → grace → suspend → deactivate. Top-ups are the existing payment-credit
flow. See docs/designs/PREPAID_DRAWDOWN_ENGINE.md.

Gated by ``billing_enabled`` at the task layer — inert until cutover.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.common import round_money

logger = logging.getLogger(__name__)

# Memo prefix for drawdown debits (used for idempotency / audit lookups).
PREPAID_CHARGE_MEMO_PREFIX = "Prepaid charge"

# Memo on the one-time cutover opening-balance credit. Its presence flips a
# Splynx-linked account's balance source from the synced deposit (#247) to the
# local ledger — see _resolve_prepaid_available_balance and the cutover runbook.
PREPAID_OPENING_BALANCE_MEMO = "Prepaid opening balance @ cutover"


def _parse_period_days(prepaid_period: str | None) -> int:
    """Map an offer's free-text ``prepaid_period`` to a charge cadence in days.

    Splynx "Prepaid (Daily)" → 1; "Prepaid (Custom)"/monthly → 30. A bare
    integer is taken as a day count. Unknown/empty defaults to 30 (covers the
    ~98%-majority ``prepaid_monthly``).
    """
    raw = (prepaid_period or "").strip().lower()
    if not raw:
        return 30
    if raw in ("daily", "day", "1d"):
        return 1
    if raw in ("weekly", "week"):
        return 7
    if raw in ("monthly", "month", "30d"):
        return 30
    try:
        n = int(raw.split()[0])
    except ValueError:
        return 30
    return n if n > 0 else 30


def _monthly_equivalent(amount: Decimal, cycle: BillingCycle | None) -> Decimal:
    """Normalise a per-cycle price to a 30-day monthly-equivalent amount."""
    amt = Decimal(str(amount))
    if cycle == BillingCycle.daily:
        return amt * Decimal("30")
    if cycle == BillingCycle.weekly:
        return amt * Decimal("30") / Decimal("7")
    if cycle == BillingCycle.annual:
        return amt / Decimal("12")
    # monthly (and unknown) treated as the monthly amount
    return amt


def _period_charge(
    db: Session, subscription: Subscription, now: datetime
) -> tuple[Decimal, str, int]:
    """Return (charge, currency, period_days) for one prepaid period.

    Charge = the recurring catalog/subscription price (discounts and
    Splynx-imported ``unit_price`` overrides applied) normalised to monthly and
    pro-rated to the period: full price for a 30-day period, 1/30 for a day.
    """
    # Imported lazily to avoid a heavy import at module load and a potential
    # cycle through billing_automation.
    from app.services.billing_automation import _effective_unit_price, _resolve_price

    period_days = _parse_period_days(
        subscription.offer.prepaid_period if subscription.offer is not None else None
    )
    amount, currency, cycle = _resolve_price(db, subscription)
    has_unit_override = (
        subscription.unit_price is not None and subscription.unit_price > 0
    )
    # Splynx-migrated subscriptions often carry a per-service unit_price with no
    # separate OfferPrice row, so price off the override even when the catalog
    # amount is absent. Only when neither exists is there nothing to charge.
    if amount is None and not has_unit_override:
        return Decimal("0.00"), (currency or "NGN"), period_days
    effective = _effective_unit_price(subscription, Decimal(str(amount or 0)), now)
    monthly = _monthly_equivalent(effective, cycle)
    charge = round_money(monthly * Decimal(period_days) / Decimal("30"))
    return charge, (currency or "NGN"), period_days


def _due_prepaid_subscriptions(db: Session, now: datetime) -> list[Subscription]:
    """Active prepaid subscriptions of active subscribers that are due or new."""
    return (
        db.query(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status == SubscriberStatus.active)
        .filter(
            (Subscription.next_billing_at.is_(None))
            | (Subscription.next_billing_at <= now)
        )
        .all()
    )


def run_prepaid_charges(
    db: Session,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict:
    """Post one prepaid drawdown charge per due subscription.

    First time a subscription is seen (``next_billing_at`` is null) it is only
    *initialised* — ``next_billing_at`` is set one period out and NO charge is
    posted — so enabling the engine never retroactively bills the shadow period
    Splynx already covered. Thereafter, when due, a single period charge is
    posted and ``next_billing_at`` advances one period from now (no backlog
    catch-up, so downtime can't multiply charges). Charges are posted even when
    the balance goes negative; enforcement suspends on the threshold.
    """
    now = now or datetime.now(UTC)
    scanned = 0
    initialised = 0
    charged = 0
    skipped_zero_price = 0
    total_charged = Decimal("0.00")

    for subscription in _due_prepaid_subscriptions(db, now):
        scanned += 1

        # First sighting: initialise the cadence without charging.
        if subscription.next_billing_at is None:
            _charge, _currency, period_days = _period_charge(db, subscription, now)
            if not dry_run:
                subscription.next_billing_at = now + timedelta(days=period_days)
            initialised += 1
            continue

        charge, currency, period_days = _period_charge(db, subscription, now)
        if charge <= Decimal("0.00"):
            # Nothing to charge (e.g. unpriced offer) — still advance so we
            # don't reprocess every run.
            if not dry_run:
                subscription.next_billing_at = now + timedelta(days=period_days)
            skipped_zero_price += 1
            continue

        if not dry_run:
            offer_name = subscription.offer.name if subscription.offer else "service"
            db.add(
                LedgerEntry(
                    account_id=subscription.subscriber_id,
                    entry_type=LedgerEntryType.debit,
                    source=LedgerSource.adjustment,
                    category=LedgerCategory.internet_service,
                    amount=charge,
                    currency=currency,
                    memo=f"{PREPAID_CHARGE_MEMO_PREFIX}: {period_days}d ({offer_name})",
                )
            )
            subscription.next_billing_at = now + timedelta(days=period_days)
        charged += 1
        total_charged += charge

    if not dry_run:
        db.commit()

    summary = {
        "scanned": scanned,
        "initialised": initialised,
        "charged": charged,
        "skipped_zero_price": skipped_zero_price,
        "total_charged": str(total_charged),
        "dry_run": dry_run,
    }
    logger.info("prepaid_charges_run", extra={"summary": summary})
    return summary
