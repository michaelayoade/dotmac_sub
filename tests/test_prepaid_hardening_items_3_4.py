"""Items 3 & 4 of the prepaid deposit-is-truth alignment.

Item 3: a prepaid mid-cycle plan-change on the generic/admin path must draw the
price difference down from prepaid funding (a ledger debit), never mint an issued
invoice — matching the customer-portal instant-change path.

Item 4: the customer-facing service status must resolve billing mode through the
same collectible-subscription-derived authority as dunning/enforcement, so the
two can't disagree for a drifted/mixed account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    CreditNote,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    SubscriptionStatus,
)
from app.services.prepaid_plan_changes import (
    prepare_immediate_prepaid_plan_change,
    resolve_prepaid_plan_change,
)
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


def _prepare_upgrade(
    db,
    subscription,
    account,
    *,
    billing_mode: BillingMode,
    status: SubscriptionStatus = SubscriptionStatus.active,
):
    subscription.billing_mode = billing_mode
    subscription.status = status
    subscription.next_billing_at = datetime.now(UTC) + timedelta(days=30)
    target = CatalogOffer(
        name="Plan Change Target",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=billing_mode,
        is_active=True,
    )
    db.add(target)
    db.flush()
    db.add(
        OfferPrice(
            offer_id=target.id,
            price_type=PriceType.recurring,
            amount=Decimal("500.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    if billing_mode == BillingMode.prepaid:
        db.add(
            LedgerEntry(
                account_id=account.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("1000.00"),
                currency="NGN",
                memo="Wallet top-up",
            )
        )
    db.commit()
    preview = resolve_prepaid_plan_change(db, subscription, str(target.id))
    return prepare_immediate_prepaid_plan_change(
        db,
        subscription,
        target,
        old_offer_name="Old Plan",
        operation_key=f"hardening-{billing_mode.value}-{subscription.id}",
        expected_preview_fingerprint=preview.fingerprint,
    )


def test_prepaid_upgrade_draws_down_funding_not_invoice(
    db_session, subscription, subscriber_account
):
    prepared = _prepare_upgrade(
        db_session,
        subscription,
        subscriber_account,
        billing_mode=BillingMode.prepaid,
    )
    db_session.flush()

    # No issued invoice / AR — the charge is a prepaid-funding drawdown instead.
    assert _invoices(db_session, subscriber_account) == []
    debits = _debits(db_session, subscriber_account)
    assert len(debits) == 1
    assert debits[0].amount > Decimal("0.00")
    assert prepared.ledger_entry == debits[0]


def test_prepaid_upgrade_makes_no_credit_note(
    db_session, subscription, subscriber_account
):
    # An upgrade (net > 0) is a prepaid-funding drawdown — never a credit note.
    _prepare_upgrade(
        db_session,
        subscription,
        subscriber_account,
        billing_mode=BillingMode.prepaid,
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
    prepared = _prepare_upgrade(
        db_session,
        subscription,
        subscriber_account,
        billing_mode=BillingMode.postpaid,
    )
    db_session.flush()

    assert _invoices(db_session, subscriber_account) == []
    assert _debits(db_session, subscriber_account) == []
    assert prepared.ledger_entry is None


def test_nonactive_prepaid_plan_change_has_no_financial_effect(
    db_session, subscription, subscriber_account
):
    prepared = _prepare_upgrade(
        db_session,
        subscription,
        subscriber_account,
        billing_mode=BillingMode.prepaid,
        status=SubscriptionStatus.pending,
    )
    db_session.flush()

    assert prepared.decision.has_financial_effect is False
    assert prepared.ledger_entry is None
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
