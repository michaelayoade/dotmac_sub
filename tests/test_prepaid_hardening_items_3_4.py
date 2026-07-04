"""Items 3 & 4 of the prepaid deposit-is-truth alignment.

Item 3: a prepaid mid-cycle plan-change on the generic/admin path must draw the
price difference down from the wallet (a ledger debit), never mint an issued
invoice — matching the customer-portal instant-change path.

Item 4: the customer-facing service status must resolve billing mode through the
same collectible-subscription-derived authority as dunning/enforcement, so the
two can't disagree for a drifted/mixed account.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import CreditNote, Invoice, LedgerEntry, LedgerEntryType
from app.models.catalog import BillingMode, SubscriptionStatus
from app.services.catalog.subscriptions import _generate_proration_invoice
from app.services.service_status import build_service_status


def _invoices(db, account):
    return db.query(Invoice).filter(Invoice.account_id == account.id).all()


def _debits(db, account):
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.account_id == account.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .all()
    )


# --- Item 3 --------------------------------------------------------------


def test_prepaid_upgrade_draws_down_wallet_not_invoice(
    db_session, subscription, subscriber_account
):
    subscription.billing_mode = BillingMode.prepaid
    db_session.commit()

    _generate_proration_invoice(
        db_session,
        subscription,
        {"net_amount": Decimal("500.00"), "days_remaining": 10},
        "Old Plan",
        "New Plan",
    )
    db_session.flush()

    # No issued invoice / AR — the charge is a wallet drawdown instead.
    assert _invoices(db_session, subscriber_account) == []
    debits = _debits(db_session, subscriber_account)
    assert len(debits) == 1
    assert debits[0].amount == Decimal("500.00")


def test_prepaid_upgrade_makes_no_credit_note(
    db_session, subscription, subscriber_account
):
    # An upgrade (net > 0) is a pure wallet drawdown — never a credit note.
    subscription.billing_mode = BillingMode.prepaid
    db_session.commit()

    _generate_proration_invoice(
        db_session,
        subscription,
        {"net_amount": Decimal("500.00"), "days_remaining": 10},
        "Old Plan",
        "New Plan",
    )
    db_session.flush()

    assert (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == subscriber_account.id)
        .count()
        == 0
    )


def test_postpaid_plan_change_generates_nothing_here(
    db_session, subscription, subscriber_account
):
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()

    _generate_proration_invoice(
        db_session,
        subscription,
        {"net_amount": Decimal("500.00"), "days_remaining": 10},
        "Old Plan",
        "New Plan",
    )
    db_session.flush()

    assert _invoices(db_session, subscriber_account) == []
    assert _debits(db_session, subscriber_account) == []


# --- Item 4 --------------------------------------------------------------


def test_service_status_uses_effective_mode_when_account_flag_drifts(
    db_session, subscription, subscriber_account
):
    # Account flag says postpaid, but the collectible subscription is prepaid.
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))
    # Prepaid-wins effective resolution → customer view agrees with enforcement.
    assert resp.billing_mode == BillingMode.prepaid.value


def test_service_status_falls_back_to_account_flag_without_subscriptions(
    db_session, subscriber_account
):
    subscriber_account.billing_mode = BillingMode.postpaid
    db_session.commit()

    resp = build_service_status(db_session, str(subscriber_account.id))
    assert resp.billing_mode == BillingMode.postpaid.value
