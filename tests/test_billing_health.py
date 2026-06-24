"""Billing liveness/anomaly monitoring signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    BillingRun,
    BillingRunStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import billing_health
from app.services.billing_health import BillingHealthSnapshot


def _subscriber(
    db, status=SubscriberStatus.active, email="hmon@example.com"
) -> Subscriber:
    s = Subscriber(
        first_name="Health",
        last_name="Mon",
        email=email,
        status=status,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name="Health Offer",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.postpaid,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscription(
    db,
    subscriber,
    offer,
    status=SubscriptionStatus.active,
    billing_mode=BillingMode.postpaid,
) -> Subscription:
    s = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=billing_mode,
    )
    db.add(s)
    db.flush()
    return s


def _add_payments(
    db, n: int, paid_at: datetime, status=PaymentStatus.succeeded
) -> None:
    for _ in range(n):
        db.add(Payment(amount=Decimal("100"), status=status, paid_at=paid_at))
    db.commit()


# ---- paid-with-balance ----------------------------------------------------


def test_paid_with_balance_counts_only_nonzero(db_session):
    sub = _subscriber(db_session)
    db_session.add(
        Invoice(account_id=sub.id, status=InvoiceStatus.paid, balance_due=Decimal("50"))
    )
    db_session.add(
        Invoice(account_id=sub.id, status=InvoiceStatus.paid, balance_due=Decimal("0"))
    )
    # an overdue-with-balance invoice must NOT count (only status=paid does)
    db_session.add(
        Invoice(
            account_id=sub.id, status=InvoiceStatus.overdue, balance_due=Decimal("99")
        )
    )
    db_session.commit()
    count, total = billing_health.paid_with_balance(db_session)
    assert count == 1
    assert total == Decimal("50")


# ---- payment volume (real 7-day baseline) ---------------------------------


def test_payment_volume_collapse_detected(db_session):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _add_payments(db_session, 35, now - timedelta(days=3))  # baseline: 35/7 = 5.0/day
    _add_payments(db_session, 1, now - timedelta(hours=2))  # last 24h: 1
    c24, avg7, ratio, collapsed = billing_health.payment_volume(db_session, now=now)
    assert c24 == 1
    assert avg7 == 5.0
    assert ratio == 0.2
    assert collapsed is True


def test_payment_volume_healthy_not_flagged(db_session):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _add_payments(db_session, 35, now - timedelta(days=3))  # avg 5.0/day
    _add_payments(db_session, 4, now - timedelta(hours=2))  # ratio 0.8
    _, _, ratio, collapsed = billing_health.payment_volume(db_session, now=now)
    assert ratio == 0.8
    assert collapsed is False


def test_payment_volume_low_baseline_never_collapses(db_session):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _add_payments(db_session, 14, now - timedelta(days=3))  # avg 2.0/day (< floor 5)
    # zero in last 24h, but baseline too low to call it a collapse
    _, avg7, ratio, collapsed = billing_health.payment_volume(db_session, now=now)
    assert avg7 == 2.0
    assert ratio == 0.0
    assert collapsed is False


# ---- scan coverage None-guards --------------------------------------------


def test_invoice_scan_coverage_no_runs(db_session):
    last_scanned, eligible, ratio = billing_health.invoice_scan_coverage(db_session)
    assert last_scanned is None
    assert ratio is None  # no run -> no ratio (never a false 0.0 alert)


def test_invoice_scan_ratio_from_latest_run(db_session):
    db_session.add(
        BillingRun(
            run_at=datetime.now(UTC),
            subscriptions_scanned=3,
            status=BillingRunStatus.success,
        )
    )
    db_session.commit()
    last_scanned, eligible, ratio = billing_health.invoice_scan_coverage(db_session)
    assert last_scanned == 3
    # eligible active subs is 0 in this isolated test -> ratio guarded to None
    assert ratio is None


def test_invoice_scan_coverage_counts_blocked_subscriber_subs(db_session):
    """run_invoice_cycle still bills active subs of blocked/suspended/delinquent
    accounts, so the coverage denominator must count them too (else blocked subs
    are scanned but not eligible -> ratio inflated, real cohort drop hidden)."""
    offer = _offer(db_session)
    active_acct = _subscriber(db_session, SubscriberStatus.active, "a@example.com")
    blocked_acct = _subscriber(db_session, SubscriberStatus.blocked, "b@example.com")
    suspended_acct = _subscriber(
        db_session, SubscriberStatus.suspended, "s@example.com"
    )
    delinquent = _subscriber(db_session, SubscriberStatus.delinquent, "d@example.com")
    # disabled account is NOT billable -> must be excluded
    disabled_acct = _subscriber(db_session, SubscriberStatus.disabled, "x@example.com")
    _subscription(db_session, active_acct, offer)
    _subscription(db_session, blocked_acct, offer)
    _subscription(db_session, suspended_acct, offer)
    _subscription(db_session, delinquent, offer)
    _subscription(db_session, disabled_acct, offer)
    # prepaid active sub is skipped by the cycle -> must be excluded
    prepaid_acct = _subscriber(db_session, SubscriberStatus.active, "p@example.com")
    _subscription(db_session, prepaid_acct, offer, billing_mode=BillingMode.prepaid)
    db_session.add(
        BillingRun(
            run_at=datetime.now(UTC),
            subscriptions_scanned=4,
            status=BillingRunStatus.success,
        )
    )
    db_session.commit()

    last_scanned, eligible, ratio = billing_health.invoice_scan_coverage(db_session)
    assert last_scanned == 4
    # active + blocked + suspended + delinquent (non-prepaid) = 4; disabled and
    # prepaid excluded.
    assert eligible == 4
    assert ratio == 1.0


# ---- anomaly thresholds (pure) --------------------------------------------


def _snap(**kw) -> BillingHealthSnapshot:
    base = dict(
        paid_with_balance_count=0,
        paid_with_balance_total=Decimal("0"),
        last_scanned=100,
        eligible_active_subs=100,
        scan_ratio=1.0,
        payments_24h=10,
        payments_7d_daily_avg=10.0,
        payment_volume_ratio=1.0,
        payment_volume_collapsed=False,
    )
    base.update(kw)
    return BillingHealthSnapshot(**base)


def test_anomalies_none_when_healthy():
    assert _snap().anomalies == []


def test_anomalies_flag_each_signal():
    assert "paid_invoices_with_balance" in _snap(paid_with_balance_count=3).anomalies
    assert "invoice_scan_count_low" in _snap(scan_ratio=0.1).anomalies
    assert "payment_volume_collapse" in _snap(payment_volume_collapsed=True).anomalies
    # scan_ratio just above the floor is fine
    assert "invoice_scan_count_low" not in _snap(scan_ratio=0.6).anomalies
