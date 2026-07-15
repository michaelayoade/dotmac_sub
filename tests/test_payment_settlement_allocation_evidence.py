from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.schemas.billing import (
    PaymentAllocationConfirm,
    PaymentAllocationCreate,
    PaymentAllocationPreviewRequest,
    PaymentCreate,
    PaymentSettlementReconciliationRequest,
)
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance
from app.services.customer_financial_ledger import calculate_customer_balance


def _invoice(db, subscriber, amount: str = "100.00") -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid4().hex[:10]}",
        status=InvoiceStatus.issued,
        subtotal=Decimal(amount),
        total=Decimal(amount),
        balance_due=Decimal(amount),
        currency="NGN",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def test_pending_payment_is_intent_until_previewed_settlement(db_session, subscriber):
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.pending,
        ),
    )

    assert payment.settlement is None
    assert payment.allocations == []
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .count()
        == 0
    )

    preview = billing_service.payments.preview_settlement(db_session, str(payment.id))
    result = billing_service.payments.settle(
        db_session,
        str(payment.id),
        preview_fingerprint=preview.fingerprint,
        idempotency_key="manual-settlement-00000001",
        origin=PaymentSettlementOrigin.manual,
    )

    assert result.payment.status == PaymentStatus.succeeded
    assert result.settlement is not None
    assert result.settlement.unallocated_ledger_entry_id is not None
    assert result.settlement.unallocated_amount == Decimal("100.00")
    assert result.settlement.origin == PaymentSettlementOrigin.manual
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "settle")
        .filter(AuditEvent.entity_id == str(payment.id))
        .count()
        == 1
    )


def test_pending_payment_rejects_embedded_allocations(db_session, subscriber):
    invoice = _invoice(db_session, subscriber)

    with pytest.raises(HTTPException) as exc:
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("80.00"),
                currency="NGN",
                status=PaymentStatus.pending,
                allocations=[{"invoice_id": invoice.id, "amount": Decimal("80.00")}],
            ),
        )

    assert exc.value.status_code == 409
    assert "intent only" in str(exc.value.detail)
    assert db_session.query(Payment).count() == 0
    assert db_session.query(PaymentAllocation).count() == 0
    assert db_session.query(LedgerEntry).count() == 0


def test_failed_payment_observation_posts_no_money(db_session, subscriber):
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("80.00"),
            currency="NGN",
            status=PaymentStatus.failed,
        ),
    )

    assert payment.settlement is None
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .count()
        == 0
    )


def test_allocation_transfers_credit_with_two_exact_ledger_links(
    db_session, subscriber
):
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )
    invoice = _invoice(db_session, subscriber, "60.00")
    funding_before = calculate_customer_balance(db_session, subscriber.id)
    credit_before = get_account_credit_balance(db_session, str(subscriber.id))
    request = PaymentAllocationPreviewRequest(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal("40.00"),
    )
    preview = billing_service.payment_allocations.preview(db_session, request)
    result = billing_service.payment_allocations.confirm(
        db_session,
        PaymentAllocationConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key="payment-allocation-00000001",
        ),
    )
    replay = billing_service.payment_allocations.confirm(
        db_session,
        PaymentAllocationConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key="payment-allocation-00000001",
        ),
    )

    allocation = result.allocation
    assert replay.idempotent_replay is True
    assert replay.allocation.id == allocation.id
    assert allocation.ledger_entry_id is not None
    assert allocation.consumption_ledger_entry_id is not None
    invoice_entry = db_session.get(LedgerEntry, allocation.ledger_entry_id)
    consumption = db_session.get(LedgerEntry, allocation.consumption_ledger_entry_id)
    assert invoice_entry is not None
    assert invoice_entry.entry_type == LedgerEntryType.credit
    assert invoice_entry.source == LedgerSource.payment
    assert invoice_entry.invoice_id == invoice.id
    assert consumption is not None
    assert consumption.entry_type == LedgerEntryType.debit
    assert consumption.source == LedgerSource.other
    assert consumption.invoice_id is None
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("20.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == (
        credit_before - Decimal("40.00")
    )
    assert calculate_customer_balance(db_session, subscriber.id) == funding_before
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "allocate_payment_credit")
        .filter(AuditEvent.entity_id == str(allocation.id))
        .count()
        == 1
    )


def test_settled_allocation_requires_preview_and_confirmation(db_session, subscriber):
    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )
    invoice = _invoice(db_session, subscriber)

    with pytest.raises(HTTPException) as exc:
        billing_service.payment_allocations.create(
            db_session,
            PaymentAllocationCreate(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=Decimal("20.00"),
            ),
        )
    assert exc.value.status_code == 409


def test_historical_reconciliation_attaches_only_selected_existing_evidence(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, "70.00")
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.flush()
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal("70.00"),
    )
    invoice_entry = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("70.00"),
        currency="NGN",
    )
    unallocated_entry = LedgerEntry(
        account_id=subscriber.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("30.00"),
        currency="NGN",
    )
    db_session.add_all([allocation, invoice_entry, unallocated_entry])
    db_session.commit()
    ledger_count = db_session.query(LedgerEntry).count()

    inspection = billing_service.payments.inspect_settlement_evidence(
        db_session, str(payment.id)
    )
    assert inspection["already_reconciled"] is False
    settlement = billing_service.payments.reconcile_settlement_evidence(
        db_session,
        str(payment.id),
        PaymentSettlementReconciliationRequest(
            allocation_ledger_entry_ids={allocation.id: invoice_entry.id},
            unallocated_ledger_entry_id=unallocated_entry.id,
            reason="Reviewed against imported bank and invoice evidence",
        ),
    )

    assert db_session.query(LedgerEntry).count() == ledger_count
    assert db_session.query(PaymentSettlement).count() == 1
    assert settlement.unallocated_ledger_entry_id == unallocated_entry.id
    assert allocation.ledger_entry_id == invoice_entry.id
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "reconcile_settlement_evidence")
        .filter(AuditEvent.entity_id == str(payment.id))
        .one()
    )
    assert audit.metadata_["money_posted"] is False
