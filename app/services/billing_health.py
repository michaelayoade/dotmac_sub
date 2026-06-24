"""Billing liveness / anomaly signals for monitoring (metrics + alerts).

These are MONITORING checks — surfaced as Prometheus gauges and operator
alerts. They are deliberately NOT enforcement gates: they never block a
suspension (that is ``billing_enforcement_guards``). They answer "is the billing
system producing correct, complete output?", not "is it safe to cut this
customer now?".

Signals:
* paid-with-balance — invoices marked ``paid`` that still carry a non-zero
  ``balance_due`` (AR-integrity defect; dashboards/restore logic trust ``paid``).
* invoice-scan coverage — the latest billing run's ``subscriptions_scanned`` vs
  the count of active subscriptions; a sharp drop means the cycle silently
  stopped scanning a cohort (the 4,041->108 incident).
* payment-volume — last-24h succeeded payments vs the trailing-7-day daily
  average; a collapse means intake broke (the Splynx-cutover recording gap),
  using a REAL baseline rather than a static floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingRun,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus

# Subscriber (account) states whose ACTIVE subscriptions are still billed by
# run_invoice_cycle — network/enforcement blocks don't suppress invoicing, so a
# non-payment ``blocked`` (or suspended/delinquent) account is scanned. The
# scan-coverage denominator MUST mirror this eligibility set, otherwise blocked
# subs are scanned but not counted as eligible, inflating the ratio and hiding a
# real cohort drop (false negative). Keep in sync with
# app/services/billing_automation.run_invoice_cycle's billable_account_statuses.
_BILLABLE_SUBSCRIBER_STATUSES = (
    SubscriberStatus.active,
    SubscriberStatus.blocked,
    SubscriberStatus.suspended,
    SubscriberStatus.delinquent,
)

# Alert thresholds. Conservative defaults; tune via ops experience.
SCAN_MIN_RATIO = 0.5  # alert if a run scanned < 50% of active subs
PAYMENT_VOLUME_MIN_RATIO = 0.4  # alert if last-24h volume < 40% of 7d daily avg
# Don't cry "collapse" on naturally low-traffic systems: require a real baseline.
PAYMENT_BASELINE_MIN_DAILY = 5.0


@dataclass(frozen=True)
class BillingHealthSnapshot:
    paid_with_balance_count: int
    paid_with_balance_total: Decimal
    last_scanned: int | None
    eligible_active_subs: int
    scan_ratio: float | None
    payments_24h: int
    payments_7d_daily_avg: float
    payment_volume_ratio: float | None
    payment_volume_collapsed: bool

    @property
    def anomalies(self) -> list[str]:
        out: list[str] = []
        if self.paid_with_balance_count > 0:
            out.append("paid_invoices_with_balance")
        if self.scan_ratio is not None and self.scan_ratio < SCAN_MIN_RATIO:
            out.append("invoice_scan_count_low")
        if self.payment_volume_collapsed:
            out.append("payment_volume_collapse")
        return out


def paid_with_balance(db: Session) -> tuple[int, Decimal]:
    """Count + sum of invoices that are ``paid`` yet retain a balance_due."""
    count_, total = db.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.balance_due), 0),
        )
        .where(Invoice.status == InvoiceStatus.paid)
        .where(Invoice.balance_due != 0)
    ).one()
    return int(count_ or 0), Decimal(str(total or 0))


def invoice_scan_coverage(db: Session) -> tuple[int | None, int, float | None]:
    """(last_run_scanned, eligible_active_subs, ratio)."""
    last_scanned = db.execute(
        select(BillingRun.subscriptions_scanned)
        .order_by(BillingRun.created_at.desc())
        .limit(1)
    ).scalar()
    # Mirror run_invoice_cycle's eligibility: ACTIVE subscriptions whose account
    # is in a billable state and that are not prepaid. Counting only
    # SubscriptionStatus.active (with no subscriber/billing_mode filter) both
    # missed blocked/suspended/delinquent accounts that ARE billed and counted
    # prepaid subs that are NOT — skewing the coverage ratio.
    eligible = (
        db.execute(
            select(func.count(Subscription.id))
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .where(Subscription.status == SubscriptionStatus.active)
            .where(Subscriber.status.in_(_BILLABLE_SUBSCRIBER_STATUSES))
            .where(Subscription.billing_mode != BillingMode.prepaid)
        ).scalar()
        or 0
    )
    ratio = (
        float(last_scanned) / float(eligible)
        if last_scanned is not None and eligible > 0
        else None
    )
    return (
        int(last_scanned) if last_scanned is not None else None,
        int(eligible),
        ratio,
    )


def payment_volume(
    db: Session, now: datetime | None = None
) -> tuple[int, float, float | None, bool]:
    """(count_24h, daily_avg_prev_7d, ratio, collapsed) for succeeded payments."""
    now = now or datetime.now(UTC)
    last_24h = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=8)  # 7-day window ending 24h ago

    count_24h = (
        db.execute(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.paid_at >= last_24h)
        ).scalar()
        or 0
    )
    count_prev_7d = (
        db.execute(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.paid_at >= baseline_start)
            .where(Payment.paid_at < last_24h)
        ).scalar()
        or 0
    )
    daily_avg = float(count_prev_7d) / 7.0
    ratio = float(count_24h) / daily_avg if daily_avg > 0 else None
    collapsed = (
        daily_avg >= PAYMENT_BASELINE_MIN_DAILY
        and ratio is not None
        and ratio < PAYMENT_VOLUME_MIN_RATIO
    )
    return int(count_24h), daily_avg, ratio, collapsed


def billing_health_snapshot(
    db: Session, now: datetime | None = None
) -> BillingHealthSnapshot:
    pwb_count, pwb_total = paid_with_balance(db)
    last_scanned, eligible, scan_ratio = invoice_scan_coverage(db)
    c24, avg7, ratio, collapsed = payment_volume(db, now=now)
    return BillingHealthSnapshot(
        paid_with_balance_count=pwb_count,
        paid_with_balance_total=pwb_total,
        last_scanned=last_scanned,
        eligible_active_subs=eligible,
        scan_ratio=scan_ratio,
        payments_24h=c24,
        payments_7d_daily_avg=avg7,
        payment_volume_ratio=ratio,
        payment_volume_collapsed=collapsed,
    )
