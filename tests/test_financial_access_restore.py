"""Regression coverage for reason-scoped financial access restoration."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.services.account_lifecycle import get_active_locks, suspend_subscription
from app.services.collections import restore_account_services
from app.services.collections._core import _suspend_account


def _prepare_prepaid(db, account, subscription, *, min_balance: str) -> None:
    account.billing_mode = BillingMode.prepaid
    account.splynx_customer_id = None
    account.deposit = None
    account.min_balance = Decimal(min_balance)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db.commit()
    suspend_subscription(
        db,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="test:prepaid_balance",
    )
    account.prepaid_low_balance_at = datetime.now(UTC) - timedelta(days=2)
    account.prepaid_deactivation_at = datetime.now(UTC) - timedelta(days=1)
    db.commit()


def test_underfunded_payment_does_not_restore_prepaid_or_clear_timers(
    db_session, subscriber_account, subscription
):
    _prepare_prepaid(
        db_session, subscriber_account, subscription, min_balance="5000.00"
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("100.00"),
            currency="NGN",
            memo="underfunded top-up",
        )
    )
    db_session.commit()

    restored = restore_account_services(db_session, str(subscriber_account.id))

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert restored == 0
    assert subscription.status == SubscriptionStatus.suspended
    assert subscriber_account.prepaid_low_balance_at is not None
    assert subscriber_account.prepaid_deactivation_at is not None
    assert {
        lock.reason
        for lock in get_active_locks(db_session, subscription_id=str(subscription.id))
    } == {EnforcementReason.prepaid}


def test_funded_topup_restores_prepaid_and_clears_timers(
    db_session, subscriber_account, subscription
):
    _prepare_prepaid(
        db_session, subscriber_account, subscription, min_balance="5000.00"
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("1000000.00"),
            currency="NGN",
            memo="funded top-up",
        )
    )
    db_session.commit()

    restored = restore_account_services(db_session, str(subscriber_account.id))

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert restored == 1
    assert subscription.status == SubscriptionStatus.active
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert get_active_locks(db_session, subscription_id=str(subscription.id)) == []


def test_overdue_debt_prevents_payment_restore_at_owner(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-RESTORE-GATE",
        status=InvoiceStatus.overdue,
        total=Decimal("50000.00"),
        balance_due=Decimal("49900.00"),
        due_at=datetime.now(UTC) - timedelta(days=3),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.commit()
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:overdue",
    )
    db_session.commit()

    restored = restore_account_services(db_session, str(subscriber_account.id))

    db_session.refresh(subscription)
    assert restored == 0
    assert subscription.status == SubscriptionStatus.suspended
    assert {
        lock.reason
        for lock in get_active_locks(db_session, subscription_id=str(subscription.id))
    } == {EnforcementReason.overdue}


def test_suspend_owner_refuses_overdue_lock_after_debt_clears(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    suspended = _suspend_account(
        db_session,
        str(subscriber_account.id),
        reason=EnforcementReason.overdue,
        source="test:stale_dunning_snapshot",
    )

    db_session.refresh(subscription)
    assert suspended is False
    assert subscription.status == SubscriptionStatus.active
    assert get_active_locks(db_session, subscription_id=str(subscription.id)) == []


def test_suspend_owner_refuses_funded_prepaid_account(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    subscriber_account.min_balance = Decimal("100.00")
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("1000000.00"),
            currency="NGN",
            memo="funded before suspend",
        )
    )
    db_session.commit()

    suspended = _suspend_account(
        db_session,
        str(subscriber_account.id),
        reason=EnforcementReason.prepaid,
        source="test:stale_prepaid_snapshot",
    )

    db_session.refresh(subscription)
    assert suspended is False
    assert subscription.status == SubscriptionStatus.active
    assert get_active_locks(db_session, subscription_id=str(subscription.id)) == []
