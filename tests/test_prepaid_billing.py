"""Prepaid drawdown engine: period parsing, charge proration, run semantics,
and the ledger/deposit balance switch at cutover."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.catalog import BillingMode, SubscriptionStatus
from app.services.billing._common import get_account_credit_balance
from app.services.collections._core import _resolve_prepaid_available_balance
from app.services.prepaid_billing import (
    PREPAID_CHARGE_MEMO_PREFIX,
    PREPAID_OPENING_BALANCE_MEMO,
    _monthly_equivalent,
    _parse_period_days,
    run_prepaid_charges,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _naive(dt):
    # SQLite drops tzinfo on round-trip; compare wall-clock only.
    return dt.replace(tzinfo=None) if dt is not None else None


def _make_prepaid(db_session, subscriber_account, subscription, *, unit_price="3000"):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = Decimal(unit_price)
    subscription.next_billing_at = None
    db_session.commit()


def test_parse_period_days():
    assert _parse_period_days(None) == 30
    assert _parse_period_days("") == 30
    assert _parse_period_days("monthly") == 30
    assert _parse_period_days("daily") == 1
    assert _parse_period_days("7") == 7
    assert _parse_period_days("14 days") == 14
    assert _parse_period_days("garbage") == 30


def test_monthly_equivalent_normalises_cycle():
    from app.models.catalog import BillingCycle

    assert _monthly_equivalent(Decimal("10"), BillingCycle.daily) == Decimal("300")
    assert _monthly_equivalent(Decimal("1200"), BillingCycle.annual) == Decimal("100")
    assert _monthly_equivalent(Decimal("3000"), BillingCycle.monthly) == Decimal("3000")
    assert _monthly_equivalent(Decimal("3000"), None) == Decimal("3000")


def test_first_run_initialises_without_charging(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription)
    summary = run_prepaid_charges(db_session, dry_run=False, now=NOW)
    assert summary["initialised"] == 1
    assert summary["charged"] == 0
    # No debit posted; balance untouched.
    assert get_account_credit_balance(db_session, str(subscriber_account.id)) == (
        Decimal("0.00")
    )
    db_session.refresh(subscription)
    assert _naive(subscription.next_billing_at) == _naive(NOW + timedelta(days=30))


def test_due_subscription_is_charged_and_advances(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription, unit_price="3000")
    subscription.next_billing_at = NOW - timedelta(days=1)  # due
    db_session.commit()

    summary = run_prepaid_charges(db_session, dry_run=False, now=NOW)
    assert summary["charged"] == 1
    assert summary["total_charged"] == "3000.00"

    # A debit ledger entry was posted for the full monthly amount (30d period).
    debit = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber_account.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .one()
    )
    assert debit.amount == Decimal("3000.00")
    assert debit.memo.startswith(PREPAID_CHARGE_MEMO_PREFIX)
    db_session.refresh(subscription)
    assert _naive(subscription.next_billing_at) == _naive(NOW + timedelta(days=30))


def test_idempotent_within_period(db_session, subscriber_account, subscription):
    _make_prepaid(db_session, subscriber_account, subscription)
    subscription.next_billing_at = NOW - timedelta(days=1)
    db_session.commit()

    run_prepaid_charges(db_session, dry_run=False, now=NOW)
    # Second run on the same day: next_billing_at is now in the future -> no charge.
    summary2 = run_prepaid_charges(db_session, dry_run=False, now=NOW)
    assert summary2["charged"] == 0
    assert summary2["scanned"] == 0  # not even selected (not due)
    debits = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber_account.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
    )
    assert debits == 1


def test_idempotency_guard_blocks_duplicate_same_day(
    db_session, subscriber_account, subscription
):
    """Even if the cursor is reset (simulating a concurrent run that saw the
    subscription due before the first advanced it), the (subscription, day)
    marker prevents a second charge."""
    _make_prepaid(db_session, subscriber_account, subscription, unit_price="3000")
    subscription.next_billing_at = NOW - timedelta(days=1)
    db_session.commit()

    run_prepaid_charges(db_session, dry_run=False, now=NOW)
    # Force the subscription back to "due", as a racing run would have seen it.
    subscription.next_billing_at = NOW - timedelta(days=1)
    db_session.commit()

    summary = run_prepaid_charges(db_session, dry_run=False, now=NOW)
    assert summary["charged"] == 0
    assert summary["skipped_duplicate"] == 1
    debits = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber_account.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
    )
    assert debits == 1  # not 2


def test_dry_run_posts_nothing(db_session, subscriber_account, subscription):
    _make_prepaid(db_session, subscriber_account, subscription)
    subscription.next_billing_at = NOW - timedelta(days=1)
    db_session.commit()

    summary = run_prepaid_charges(db_session, dry_run=True, now=NOW)
    assert summary["charged"] == 1  # would charge
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber_account.id)
        .count()
        == 0
    )


def test_daily_period_charges_one_thirtieth(
    db_session, subscriber_account, subscription, catalog_offer
):
    _make_prepaid(db_session, subscriber_account, subscription, unit_price="3000")
    catalog_offer.prepaid_period = "daily"
    subscription.next_billing_at = NOW - timedelta(days=1)
    db_session.commit()

    run_prepaid_charges(db_session, dry_run=False, now=NOW)
    debit = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == subscriber_account.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .one()
    )
    assert debit.amount == Decimal("100.00")  # 3000 / 30
    db_session.refresh(subscription)
    assert _naive(subscription.next_billing_at) == _naive(NOW + timedelta(days=1))


def test_resolver_uses_deposit_until_seeded_then_ledger(
    db_session, subscriber_account, subscription
):
    """Migrated account: deposit drives the balance until the cutover seed exists,
    then the ledger (so drawdown debits/top-ups take effect)."""
    subscriber_account.splynx_customer_id = 4242
    subscriber_account.deposit = Decimal("5000.00")
    subscription.billing_mode = BillingMode.prepaid
    db_session.commit()

    # Unseeded: returns the synced deposit, ignoring the ledger.
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("123.00"),
            currency="NGN",
            memo="stray credit",
        )
    )
    db_session.commit()
    assert _resolve_prepaid_available_balance(
        db_session, str(subscriber_account.id)
    ) == Decimal("5000.00")

    # Seed the opening balance -> resolver switches to the ledger.
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("5000.00"),
            currency="NGN",
            memo=PREPAID_OPENING_BALANCE_MEMO,
        )
    )
    db_session.commit()
    # Ledger now = 123 stray + 5000 seed = 5123, minus no open invoices.
    assert _resolve_prepaid_available_balance(
        db_session, str(subscriber_account.id)
    ) == Decimal("5123.00")


def test_billing_switch_guard_detects_drift(db_session):
    """Guard flags when billing_enabled != the pinned expected value."""
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.services.billing_settings import check_billing_switch

    def _set(key: str, value: str):
        db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.billing, DomainSetting.key == key
        ).delete()
        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key=key,
                value_text=value,
                is_active=True,
            )
        )
        db_session.commit()

    # Pre-cutover: switch off, expected off (default) -> ok.
    _set("billing_enabled", "false")
    assert check_billing_switch(db_session) == {
        "ok": True,
        "expected": False,
        "actual": False,
    }

    # Flip billing_enabled true while expected stays false -> drift.
    _set("billing_enabled", "true")
    r = check_billing_switch(db_session)
    assert r["actual"] is True and r["expected"] is False and r["ok"] is False

    # Pin expected true (cutover) -> ok again.
    _set("billing_enabled_expected", "true")
    assert check_billing_switch(db_session)["ok"] is True


def test_drawdown_reduces_seeded_balance(db_session, subscriber_account, subscription):
    """End-to-end: after seeding, a drawdown debit lowers the resolved balance
    that enforcement reads."""
    subscriber_account.splynx_customer_id = 4243
    subscriber_account.deposit = Decimal("3000.00")
    _make_prepaid(db_session, subscriber_account, subscription, unit_price="3000")
    subscription.next_billing_at = NOW - timedelta(days=1)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("3000.00"),
            currency="NGN",
            memo=PREPAID_OPENING_BALANCE_MEMO,
        )
    )
    db_session.commit()
    assert _resolve_prepaid_available_balance(
        db_session, str(subscriber_account.id)
    ) == Decimal("3000.00")

    run_prepaid_charges(db_session, dry_run=False, now=NOW)
    # 3000 seed - 3000 charge = 0 available.
    assert _resolve_prepaid_available_balance(
        db_session, str(subscriber_account.id)
    ) == Decimal("0.00")
