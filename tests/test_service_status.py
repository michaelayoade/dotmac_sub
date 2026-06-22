"""Tests for the truthful /me/service-status builder.

Service expiry is status/balance-driven, not date-driven: prepaid lapses on
balance exhaustion (grace/deactivation timers), postpaid only via dunning.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.service_status import build_service_status


def _n(dt):
    # SQLite (test DB) drops tzinfo on tz-aware columns; Postgres keeps it.
    # Normalise so assertions compare the instant, not the tz attribute.
    return dt.replace(tzinfo=None) if dt is not None else None


def _activate(db, subscription, mode):
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    db.commit()


def test_postpaid_active_no_overdue_is_ok_with_no_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.next_billing_at = datetime.now(UTC) - timedelta(days=3)
    _activate(db_session, subscription, BillingMode.postpaid)

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.billing_mode == "postpaid"
    assert resp.in_dunning is False
    assert resp.outstanding == Decimal("0.00")
    assert len(resp.services) == 1
    svc = resp.services[0]
    assert svc.usable is True
    assert svc.reason == "ok"
    # A stale next_billing_at must NOT become an expiry for postpaid.
    assert svc.expires_at is None
    assert svc.next_charge_at == subscription.next_billing_at


def test_postpaid_overdue_invoice_flags_dunning(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    _activate(db_session, subscription, BillingMode.postpaid)
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.issued,
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            issued_at=datetime.now(UTC) - timedelta(days=30),
            due_at=datetime.now(UTC) - timedelta(days=10),
        ),
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.in_dunning is True
    assert resp.outstanding == Decimal("5000.00")
    assert resp.oldest_overdue_due_at is not None
    assert resp.services[0].reason == "overdue"


def test_prepaid_healthy_balance_is_ok_no_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = Decimal("100.00")
    _activate(db_session, subscription, BillingMode.prepaid)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("500.00"),
            currency="NGN",
            memo="top-up",
        )
    )
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.billing_mode == "prepaid"
    assert resp.balance == Decimal("500.00")
    assert resp.low_balance is False
    assert resp.grace_until is None
    svc = resp.services[0]
    assert svc.reason == "ok"
    assert svc.expires_at is None


def test_prepaid_low_balance_surfaces_grace_as_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = Decimal("100.00")
    subscriber_account.grace_period_days = 3
    low_at = datetime.now(UTC) - timedelta(days=1)
    subscriber_account.prepaid_low_balance_at = low_at
    _activate(db_session, subscription, BillingMode.prepaid)  # balance 0 < 100

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.low_balance is True
    assert _n(resp.grace_until) == _n(low_at + timedelta(days=3))
    svc = resp.services[0]
    assert svc.reason == "low_balance"
    # The real pending lapse is when grace ends (then suspension), not a bill date.
    assert _n(svc.expires_at) == _n(low_at + timedelta(days=3))


def test_contract_end_at_always_wins_as_expiry(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    end = datetime.now(UTC) + timedelta(days=20)
    subscription.end_at = end
    _activate(db_session, subscription, BillingMode.postpaid)

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert _n(resp.services[0].expires_at) == _n(end)


def test_ended_subscriptions_are_excluded(db_session, subscriber_account, subscription):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.canceled
    subscription.canceled_at = datetime.now(UTC)
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))

    assert resp.services == []
