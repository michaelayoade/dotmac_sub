"""Tests for the balance/expiry-based prepaid enforcement sweep.

This sweep SUSPENDS customers, so it is gated OFF by default and every state
transition is covered here: flag-off no-op, arm+warn (no suspend on first run),
suspend after the deactivation window elapses, recovery (clear timers +
restore), idempotent re-runs, and skip-day deferral of the deactivation step.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.notification import Notification
from app.services.account_lifecycle import suspend_subscription
from app.services.collections.prepaid_balance_sweep import run_prepaid_balance_sweep

# A fixed weekday noon (UTC) so the default 08:00 blocking_time window is open
# and weekend skips don't fire unless a test asks for them. 2026-07-06 = Monday.
_MONDAY_NOON = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
_SATURDAY_NOON = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _enable_control(db, *, enabled: bool = True) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key="prepaid_balance_enforcement_enabled",
            value_type=SettingValueType.boolean,
            value_text="true" if enabled else "false",
            value_json=enabled,
            is_active=True,
        )
    )
    db.commit()


def _set_collections_setting(
    db, key: str, *, text=None, json_value=None, vtype
) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key=key,
            value_type=vtype,
            value_text=text,
            value_json=json_value,
            is_active=True,
        )
    )
    db.commit()


def _make_prepaid(db, account, subscription, *, credit: Decimal, min_balance="100.00"):
    account.billing_mode = BillingMode.prepaid
    account.splynx_customer_id = None
    account.deposit = None
    account.min_balance = Decimal(min_balance)
    account.email = account.email or "prepaid@example.com"
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    if credit > 0:
        db.add(
            LedgerEntry(
                account_id=account.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal(credit),
                currency="NGN",
                memo="top-up",
            )
        )
    db.commit()


def _notices(db, account):
    return (
        db.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .filter(Notification.recipient == account.email)
        .all()
    )


def _prepaid_locks(db, subscription):
    return (
        db.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id == subscription.id)
        .filter(EnforcementLock.reason == EnforcementReason.prepaid)
        .filter(EnforcementLock.is_active.is_(True))
        .all()
    )


# ---------------------------------------------------------------------------
# Flag OFF → complete no-op
# ---------------------------------------------------------------------------


def test_flag_off_is_noop(db_session, subscriber_account, subscription):
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result == {"skipped": "disabled"}
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    assert _notices(db_session, subscriber_account) == []


def test_subscription_prepaid_mode_drives_candidate_selection(
    db_session, subscriber_account, subscription
):
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.billing_mode = BillingMode.postpaid
    db_session.commit()
    _enable_control(db_session)

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    assert result["warned"] == 1
    assert subscriber_account.prepaid_low_balance_at is not None


# ---------------------------------------------------------------------------
# Low balance, first run → arm + warn, but do NOT suspend
# ---------------------------------------------------------------------------


def test_low_balance_first_run_arms_and_warns_without_suspend(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["warned"] == 1
    assert result["suspended"] == 0
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is not None
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    notices = _notices(db_session, subscriber_account)
    assert len(notices) == 1
    assert notices[0].subject == "Low Balance Warning"
    # Body placeholders resolved (default template mentions the threshold).
    assert "100" in notices[0].body


def test_prepaid_legacy_invoice_ar_does_not_reduce_wallet_balance(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("100.00")
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-PREPAID-PHANTOM-AR",
        status=InvoiceStatus.overdue,
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        due_at=_MONDAY_NOON - timedelta(days=10),
        metadata_={},
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid renewal",
            quantity=Decimal("1.000"),
            unit_price=Decimal("80.00"),
            amount=Decimal("80.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


def test_imported_line_less_prepaid_ar_does_not_reduce_wallet_balance(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("100.00")
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-IMPORTED-PREPAID-PHANTOM-AR",
        status=InvoiceStatus.overdue,
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        due_at=_MONDAY_NOON - timedelta(days=10),
        metadata_={"imported_via": "system_import_wizard"},
    )
    db_session.add(invoice)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


@pytest.mark.parametrize("hold_value", [True, "true", "1", "yes", "on"])
def test_reconciliation_hold_invoice_does_not_reduce_wallet_balance(
    db_session, subscriber_account, subscription, hold_value
):
    _enable_control(db_session)
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("100.00")
    )
    invoice = Invoice(
        account_id=subscriber_account.id,
        invoice_number="INV-HELD-PREPAID-AR",
        status=InvoiceStatus.overdue,
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        due_at=_MONDAY_NOON - timedelta(days=10),
        metadata_={"reconciliation_hold": hold_value},
    )
    db_session.add(invoice)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert result["ok"] == 1
    assert result["warned"] == 0
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscription.status == SubscriptionStatus.active


# ---------------------------------------------------------------------------
# Deactivation window elapsed → suspend (EnforcementReason.prepaid)
# ---------------------------------------------------------------------------


def test_suspends_after_deactivation_days_elapsed(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    # Low balance was already armed two days ago; default deactivation_days=0 so
    # it is due. (Not "just armed" this run → the deactivation branch runs.)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.suspended
    locks = _prepaid_locks(db_session, subscription)
    assert len(locks) == 1
    assert locks[0].source == "prepaid_balance_sweep"
    notices = _notices(db_session, subscriber_account)
    assert any(n.subject == "Service Deactivated" for n in notices)


def test_respects_configured_deactivation_days(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    _set_collections_setting(
        db_session,
        "prepaid_deactivation_days",
        text="5",
        vtype=SettingValueType.integer,
    )
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    # Armed only 2 days ago; 5-day window has NOT elapsed → warn stays, no suspend.
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 0
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert subscriber_account.prepaid_deactivation_at is None


# ---------------------------------------------------------------------------
# Recovery → clear timers + restore
# ---------------------------------------------------------------------------


def test_funded_recovery_clears_timers_and_restores(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    # Start suspended-for-prepaid with both timers set...
    _make_prepaid(
        db_session, subscriber_account, subscription, credit=Decimal("500.00")
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_balance_sweep",
    )
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=3)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=1)
    db_session.commit()
    assert subscription.status == SubscriptionStatus.suspended

    # ...now funded (credit 500 >= min 100).
    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["restored"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    assert _prepaid_locks(db_session, subscription) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerun_does_not_rewarn_or_double_suspend(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    db_session.commit()

    first = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    assert first["suspended"] == 1
    deact_first = subscriber_account.prepaid_deactivation_at

    second = run_prepaid_balance_sweep(
        db_session, now=_MONDAY_NOON + timedelta(hours=1)
    )

    # Second pass: already suspended + deactivation armed → no new work.
    assert second["suspended"] == 0
    assert second["warned"] == 0
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_deactivation_at == deact_first
    assert len(_prepaid_locks(db_session, subscription)) == 1
    # Exactly one deactivation notice (not duplicated on re-run).
    deact_notices = [
        n
        for n in _notices(db_session, subscriber_account)
        if n.subject == "Service Deactivated"
    ]
    assert len(deact_notices) == 1


def test_low_balance_rerun_does_not_send_second_warning(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    # 5-day window so no suspend happens; only the warning path is exercised.
    _set_collections_setting(
        db_session,
        "prepaid_deactivation_days",
        text="5",
        vtype=SettingValueType.integer,
    )
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))

    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON + timedelta(hours=6))

    warnings = [
        n
        for n in _notices(db_session, subscriber_account)
        if n.subject == "Low Balance Warning"
    ]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Skip-day / window defers the DEACTIVATION step (never the warning)
# ---------------------------------------------------------------------------


def test_skip_weekend_defers_deactivation(db_session, subscriber_account, subscription):
    _enable_control(db_session)
    _set_collections_setting(
        db_session,
        "prepaid_skip_weekends",
        json_value=True,
        text="true",
        vtype=SettingValueType.boolean,
    )
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _SATURDAY_NOON - timedelta(days=2)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_SATURDAY_NOON)

    assert result["deferred"] == 1
    assert result["suspended"] == 0
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    # Deactivation deferred: no timer armed, service stays up...
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status == SubscriptionStatus.active
    # ...and once it is a weekday, the deactivation proceeds.
    followup = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    assert followup["suspended"] == 1
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_blocking_time_window_defers_deactivation(
    db_session, subscriber_account, subscription
):
    _enable_control(db_session)
    # Only act at/after 09:00 local (UTC in tests).
    _set_collections_setting(
        db_session,
        "prepaid_blocking_time",
        text="09:00",
        vtype=SettingValueType.string,
    )
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    db_session.commit()

    before_window = _MONDAY_NOON.replace(hour=7)
    result = run_prepaid_balance_sweep(db_session, now=before_window)

    assert result["deferred"] == 1
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


# ---------------------------------------------------------------------------
# Non-prepaid accounts are ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [BillingMode.postpaid])
def test_postpaid_accounts_untouched(
    db_session, subscriber_account, subscription, mode
):
    _enable_control(db_session)
    subscriber_account.billing_mode = mode
    subscriber_account.min_balance = Decimal("100.00")
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["accounts_scanned"] == 0
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_suspend_blocked_leaves_timer_unarmed_and_retries(
    db_session, subscriber_account, subscription, monkeypatch
):
    """If the suspend is blocked (shield / dedicated bundle → _suspend_account
    returns False), prepaid_deactivation_at must NOT be armed, so the next sweep
    re-attempts rather than short-circuiting on a set-but-not-suspended timer."""
    import app.services.collections.prepaid_balance_sweep as sweep

    _enable_control(db_session)
    _make_prepaid(db_session, subscriber_account, subscription, credit=Decimal("0"))
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    db_session.commit()

    calls = {"n": 0}

    def _blocked(*args, **kwargs):
        calls["n"] += 1
        return False

    monkeypatch.setattr(sweep, "_suspend_account", _blocked)

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    assert result["suspended"] == 0
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_deactivation_at is None
    assert subscription.status != SubscriptionStatus.suspended
    assert calls["n"] == 1

    # Next run re-attempts the suspension (timer still unarmed).
    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON + timedelta(hours=1))
    assert calls["n"] == 2
