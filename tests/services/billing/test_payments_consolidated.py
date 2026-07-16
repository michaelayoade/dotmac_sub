"""Tests for consolidated (billing-account-scoped) payment allocation."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import (
    BillingAccountCreditAllocation,
    BillingAccountCreditAllocationItem,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    PaymentAllocation,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountCreditAllocationConfirm,
    BillingAccountCreditAllocationPreviewRequest,
    BillingAccountPaymentPreviewRequest,
    PaymentCreate,
)
from app.services import billing as billing_service


def _make_reseller(db_session, *, name: str = "Partner"):
    r = Reseller(name=name)
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def _make_subscriber(db_session, *, reseller_id, suffix: str):
    sub = Subscriber(
        first_name="Sub",
        last_name=suffix,
        email=f"sub-{suffix.lower()}@example.com",
        reseller_id=reseller_id,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _make_invoice(db_session, *, account_id, balance: Decimal):
    inv = Invoice(
        account_id=account_id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=balance,
        balance_due=balance,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


def _settle(db_session, payload: PaymentCreate, *, auto_allocate: bool = True):
    assert payload.billing_account_id is not None
    request = BillingAccountPaymentPreviewRequest(
        **payload.model_dump(exclude={"account_id", "billing_account_id", "status"}),
        auto_allocate=auto_allocate,
    )
    return billing_service.consolidated_payment_settlements.settle_verified(
        db_session,
        str(payload.billing_account_id),
        request,
        idempotency_key=f"test-consolidated-{uuid.uuid4()}",
        origin=PaymentSettlementOrigin.system,
    ).payment


def _allocate_credit(db_session, billing_account_id, subscriber_id, amount=None):
    request = BillingAccountCreditAllocationPreviewRequest(amount=amount)
    preview = billing_service.consolidated_credit_allocations.preview(
        db_session, str(billing_account_id), str(subscriber_id), request
    )
    result = billing_service.consolidated_credit_allocations.confirm(
        db_session,
        str(billing_account_id),
        str(subscriber_id),
        BillingAccountCreditAllocationConfirm(
            amount=amount,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=f"test-credit-allocation-{uuid.uuid4()}",
        ),
    )
    return preview, result


def test_paymentcreate_requires_exactly_one_account_scope():
    with pytest.raises(ValueError):
        PaymentCreate(amount=Decimal("10.00"))


def test_consolidated_payment_auto_allocates_across_subscribers(db_session):
    """One consolidated payment FIFO-allocates across multiple subscribers."""
    reseller = _make_reseller(db_session)
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub_a = _make_subscriber(db_session, reseller_id=reseller.id, suffix="A")
    sub_b = _make_subscriber(db_session, reseller_id=reseller.id, suffix="B")
    inv_a = _make_invoice(db_session, account_id=sub_a.id, balance=Decimal("300.00"))
    inv_b = _make_invoice(db_session, account_id=sub_b.id, balance=Decimal("200.00"))

    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("450.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )
    assert payment.billing_account_id == ba.id
    assert payment.account_id is None

    db_session.refresh(inv_a)
    db_session.refresh(inv_b)
    db_session.refresh(ba)
    allocated_total = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    allocations_by_invoice = {str(a.invoice_id): a.amount for a in allocated_total}
    # Both invoices got allocations.
    assert str(inv_a.id) in allocations_by_invoice
    assert str(inv_b.id) in allocations_by_invoice
    assert allocations_by_invoice[str(inv_a.id)] + allocations_by_invoice[
        str(inv_b.id)
    ] == Decimal("450.00")
    # Unallocated balance is zero (everything fit).
    assert ba.balance == Decimal("0.00")


def test_consolidated_payment_remainder_credits_billing_account_balance(db_session):
    reseller = _make_reseller(db_session, name="WithRemainder")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="C")
    _make_invoice(db_session, account_id=sub.id, balance=Decimal("100.00"))

    _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("250.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )
    db_session.refresh(ba)
    # 100 allocated, 150 surplus on the billing account.
    assert ba.balance == Decimal("150.00")


def test_consolidated_payment_can_remain_unallocated(db_session):
    reseller = _make_reseller(db_session, name="ManualOnly")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="MANUAL")
    inv = _make_invoice(db_session, account_id=sub.id, balance=Decimal("100.00"))

    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
        auto_allocate=False,
    )

    db_session.refresh(inv)
    db_session.refresh(ba)
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert allocations == []
    assert inv.balance_due == Decimal("100.00")
    assert ba.balance == Decimal("100.00")


def test_allocate_consolidated_balance_to_selected_subscriber(db_session):
    reseller = _make_reseller(db_session, name="ManualAllocate")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub_a = _make_subscriber(db_session, reseller_id=reseller.id, suffix="MA")
    sub_b = _make_subscriber(db_session, reseller_id=reseller.id, suffix="MB")
    inv_a = _make_invoice(db_session, account_id=sub_a.id, balance=Decimal("80.00"))
    inv_b = _make_invoice(db_session, account_id=sub_b.id, balance=Decimal("80.00"))

    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
        auto_allocate=False,
    )

    preview, result = _allocate_credit(db_session, ba.id, sub_b.id)

    db_session.refresh(inv_a)
    db_session.refresh(inv_b)
    db_session.refresh(ba)
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert preview.allocation_amount == Decimal("80.00")
    assert result.amount == Decimal("80.00")
    assert result.subscriber_id == sub_b.id
    assert len(allocations) == 1
    assert allocations[0].invoice_id == inv_b.id
    assert inv_a.balance_due == Decimal("80.00")
    assert inv_b.balance_due == Decimal("0.00")
    assert ba.balance == Decimal("20.00")


def test_allocate_consolidated_balance_never_exceeds_unallocated_credit(db_session):
    reseller = _make_reseller(db_session, name="ManualCap")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="CAP")
    inv = _make_invoice(db_session, account_id=sub.id, balance=Decimal("250.00"))

    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
        auto_allocate=False,
    )

    _preview, result = _allocate_credit(db_session, ba.id, sub.id)

    db_session.refresh(inv)
    db_session.refresh(ba)
    allocation = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .one()
    )
    assert result.amount == Decimal("100.00")
    assert allocation.amount == Decimal("100.00")
    assert inv.balance_due == Decimal("150.00")
    assert ba.balance == Decimal("0.00")


def test_allocate_consolidated_balance_honors_requested_amount(db_session):
    reseller = _make_reseller(db_session, name="ManualPartial")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="PART")
    inv = _make_invoice(db_session, account_id=sub.id, balance=Decimal("250.00"))

    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
        auto_allocate=False,
    )

    _preview, result = _allocate_credit(
        db_session, ba.id, sub.id, amount=Decimal("40.00")
    )

    db_session.refresh(inv)
    db_session.refresh(ba)
    allocation = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .one()
    )
    assert result.amount == Decimal("40.00")
    assert allocation.amount == Decimal("40.00")
    assert inv.balance_due == Decimal("210.00")
    assert ba.balance == Decimal("60.00")


def test_allocate_consolidated_balance_rejects_unbacked_projection_credit(db_session):
    reseller = _make_reseller(db_session, name="BalanceOnly")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="ONLY")
    inv = _make_invoice(db_session, account_id=sub.id, balance=Decimal("30000.00"))
    ba.balance = Decimal("30000.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="unbacked balance drift"):
        billing_service.consolidated_credit_allocations.preview(
            db_session,
            str(ba.id),
            str(sub.id),
            BillingAccountCreditAllocationPreviewRequest(),
        )

    db_session.refresh(inv)
    db_session.refresh(ba)
    assert db_session.query(PaymentAllocation).count() == 0
    assert db_session.query(BillingAccountCreditAllocation).count() == 0
    assert inv.balance_due == Decimal("30000.00")
    assert ba.balance == Decimal("30000.00")


def test_reseller_account_activity_includes_subscriber_allocations(db_session):
    from app.services import reseller_portal_billing

    reseller = _make_reseller(db_session, name="Activity")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="ACT")
    inv = _make_invoice(db_session, account_id=sub.id, balance=Decimal("100.00"))

    _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
        auto_allocate=False,
    )
    _allocate_credit(db_session, ba.id, sub.id)

    activity = reseller_portal_billing.account_activity(db_session, str(reseller.id))

    allocation_entries = [
        entry for entry in activity if entry["title"] == "Funds allocated"
    ]
    assert allocation_entries
    assert allocation_entries[0]["amount"] == Decimal("100.00")
    assert "Sub ACT" in allocation_entries[0]["description"]
    assert str(inv.id) in allocation_entries[0]["description"]


def test_credit_allocation_links_source_and_resulting_ledger_evidence(db_session):
    reseller = _make_reseller(db_session, name="ExactEvidence")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub = _make_subscriber(db_session, reseller_id=reseller.id, suffix="EVIDENCE")
    invoice = _make_invoice(db_session, account_id=sub.id, balance=Decimal("75.00"))
    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
        auto_allocate=False,
    )

    preview, result = _allocate_credit(
        db_session, ba.id, sub.id, amount=Decimal("60.00")
    )

    allocation = db_session.get(BillingAccountCreditAllocation, result.allocation_id)
    item = db_session.query(BillingAccountCreditAllocationItem).one()
    payment_allocation = db_session.get(PaymentAllocation, item.payment_allocation_id)
    assert allocation is not None
    assert allocation.billing_account_ledger_entry_id == (
        result.billing_account_ledger_entry_id
    )
    assert item.source_billing_account_ledger_entry_id == (
        preview.source_effects[0].billing_account_ledger_entry_id
    )
    assert payment_allocation is not None
    assert payment_allocation.payment_id == payment.id
    assert payment_allocation.invoice_id == invoice.id
    assert payment_allocation.ledger_entry_id == item.subscriber_ledger_entry_id
    assert result.payment_allocation_ids == [item.payment_allocation_id]
    assert result.subscriber_ledger_entry_ids == [item.subscriber_ledger_entry_id]


def test_billing_account_statement_filters_open_invoice_subscribers(db_session):
    reseller = _make_reseller(db_session, name="SearchStatement")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    alpha = _make_subscriber(db_session, reseller_id=reseller.id, suffix="Alpha")
    beta = _make_subscriber(db_session, reseller_id=reseller.id, suffix="Beta")
    _make_invoice(db_session, account_id=alpha.id, balance=Decimal("50.00"))
    _make_invoice(db_session, account_id=beta.id, balance=Decimal("75.00"))

    statement = billing_service.billing_accounts.statement(
        db_session, str(ba.id), subscriber_search="beta"
    )

    assert statement.total_outstanding == Decimal("125.00")
    assert statement.subscribers_total == 2
    assert [str(row.subscriber_id) for row in statement.subscribers] == [str(beta.id)]


def test_consolidated_payment_ledger_entries_are_per_subscriber(db_session):
    reseller = _make_reseller(db_session, name="LedgerCheck")
    ba = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    sub_a = _make_subscriber(db_session, reseller_id=reseller.id, suffix="LA")
    sub_b = _make_subscriber(db_session, reseller_id=reseller.id, suffix="LB")
    _make_invoice(db_session, account_id=sub_a.id, balance=Decimal("100.00"))
    _make_invoice(db_session, account_id=sub_b.id, balance=Decimal("100.00"))

    payment = _settle(
        db_session,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=Decimal("200.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )
    entries = (
        db_session.query(LedgerEntry).filter(LedgerEntry.payment_id == payment.id).all()
    )
    # One ledger entry per subscriber.
    account_ids = {str(e.account_id) for e in entries}
    assert {str(sub_a.id), str(sub_b.id)} <= account_ids
    # And every entry has a per-subscriber account_id (none with NULL).
    assert all(e.account_id is not None for e in entries)


def test_consolidated_explicit_allocation_rejects_cross_reseller_invoice(db_session):
    r1 = _make_reseller(db_session, name="R1")
    r2 = _make_reseller(db_session, name="R2")
    ba1 = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(r1.id)
    )
    sub_other = _make_subscriber(db_session, reseller_id=r2.id, suffix="OTHER")
    inv_other = _make_invoice(
        db_session, account_id=sub_other.id, balance=Decimal("100.00")
    )

    from app.schemas.billing import PaymentAllocationApply

    with pytest.raises(HTTPException) as exc:
        _settle(
            db_session,
            PaymentCreate(
                billing_account_id=ba1.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=inv_other.id, amount=Decimal("100.00")
                    )
                ],
            ),
        )
    assert exc.value.status_code == 400


def test_account_scoped_payment_still_rejects_cross_account_invoice(
    db_session, subscriber
):
    """Regression: existing single-account flow still enforces same-account."""
    other_reseller = _make_reseller(db_session, name="Other")
    other = _make_subscriber(db_session, reseller_id=other_reseller.id, suffix="ACTUAL")
    inv_other = _make_invoice(db_session, account_id=other.id, balance=Decimal("50.00"))
    from app.schemas.billing import PaymentAllocationApply

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("50.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=inv_other.id, amount=Decimal("50.00")
                    )
                ],
            ),
        )
    assert exc.value.status_code == 400
