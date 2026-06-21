"""Tests for the paid/partial prepaid phantom classifier (read-only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.one_off.classify_paid_prepaid_phantoms import (
    classify_one,
    evidence,
    manual_review_invoices,
)


def _ev(**over):
    base = {
        "num_allocations": 1,
        "any_payment_succeeded": True,
        "all_allocated_have_credit": False,
        "splynx_linked": False,
        "deposit": "",
        "service_active": True,
    }
    base.update(over)
    return base


def test_classify_no_allocation_or_no_success_is_manual():
    assert classify_one(_ev(num_allocations=0)) == "manual_finance_review"
    assert classify_one(_ev(any_payment_succeeded=False)) == "manual_finance_review"


def test_classify_already_credited_local_ledger():
    assert classify_one(_ev(all_allocated_have_credit=True)) == "already_credited"


def test_classify_already_credited_via_splynx_deposit():
    # unseeded Splynx deposit already nets the payment; no local credit needed
    assert (
        classify_one(
            _ev(all_allocated_have_credit=False, splynx_linked=True, deposit="5000.00")
        )
        == "already_credited"
    )


def test_classify_reallocate_when_active_but_not_credited():
    assert classify_one(_ev(all_allocated_have_credit=False)) == "reallocate_candidate"


def test_classify_refund_when_no_active_service():
    # no service basis -> refund regardless of credited state
    assert (
        classify_one(_ev(service_active=False, all_allocated_have_credit=True))
        == "refund_candidate"
    )
    assert (
        classify_one(_ev(service_active=False, all_allocated_have_credit=False))
        == "refund_candidate"
    )


def _subscriber(db, splynx=None):
    s = Subscriber(
        first_name="P",
        last_name="P",
        email=f"{uuid.uuid4()}@e.com",
        status=SubscriberStatus.active,
        billing_mode=BillingMode.prepaid,
        splynx_customer_id=splynx,
    )
    db.add(s)
    db.flush()
    return s


def test_evidence_and_targeting_integration(db_session, catalog_offer):
    sub = _subscriber(db_session)
    db_session.add(
        Subscription(
            subscriber_id=sub.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
        )
    )
    now = datetime.now(UTC)
    inv = Invoice(
        account_id=sub.id,
        invoice_number=f"INV-{uuid.uuid4()}",
        status=InvoiceStatus.paid,
        subtotal=Decimal("100"),
        tax_total=Decimal("0"),
        total=Decimal("100"),
        balance_due=Decimal("0"),
        billing_period_start=now - timedelta(days=5),
        billing_period_end=now + timedelta(days=25),
        is_active=True,
    )
    db_session.add(inv)
    db_session.flush()
    pay = Payment(
        account_id=sub.id,
        amount=Decimal("100"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(pay)
    db_session.flush()
    db_session.add(
        PaymentAllocation(
            payment_id=pay.id, invoice_id=inv.id, amount=Decimal("100"), is_active=True
        )
    )
    db_session.add(
        LedgerEntry(
            account_id=sub.id,
            payment_id=pay.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("100"),
            currency="NGN",
            memo="Payment",
        )
    )
    db_session.commit()

    invs = manual_review_invoices(db_session)
    assert inv in invs
    ev = evidence(db_session, inv)
    assert ev["num_allocations"] == 1
    assert ev["any_payment_succeeded"] is True
    assert ev["all_allocated_have_credit"] is True
    assert ev["service_active"] is True
    assert classify_one(ev) == "already_credited"
