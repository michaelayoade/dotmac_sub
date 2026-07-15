from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceClosure,
    InvoiceClosureOrigin,
    InvoiceClosureType,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    PaymentStatus,
)
from app.models.catalog import BillingMode
from app.schemas.billing import (
    InvoiceClosureConfirm,
    InvoiceClosureEvidenceSelection,
    InvoiceClosureReconciliationRequest,
    InvoiceUpdate,
    LedgerEntryCreate,
    PaymentAllocationApply,
    PaymentCreate,
)
from app.services import billing as billing_service
from app.services.customer_financial_ledger import (
    calculate_customer_balance,
    customer_financial_balances_by_currency,
)


def _invoice(db, subscriber, *, status=InvoiceStatus.issued, amount="100.00"):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-CLOSURE-{uuid4().hex[:8]}",
        status=status,
        subtotal=Decimal(amount),
        total=Decimal(amount),
        balance_due=Decimal(amount),
        currency="NGN",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def test_write_off_confirms_exact_bad_debt_evidence_and_replays(db_session, subscriber):
    invoice = _invoice(db_session, subscriber, amount="120.00")
    preview = billing_service.invoices.preview_write_off(db_session, str(invoice.id))
    request = InvoiceClosureConfirm(
        preview_fingerprint=preview.fingerprint,
        idempotency_key="invoice-writeoff-evidence-0001",
        memo="Reviewed bad debt",
    )

    result = billing_service.invoices.confirm_write_off(
        db_session, str(invoice.id), request
    )
    replay = billing_service.invoices.confirm_write_off(
        db_session, str(invoice.id), request
    )

    assert replay.idempotent_replay is True
    assert replay.closure.id == result.closure.id
    assert result.invoice.status == InvoiceStatus.written_off
    assert result.invoice.balance_due == Decimal("0.00")
    assert result.invoice.paid_at is None
    assert result.closure.closure_type == InvoiceClosureType.write_off
    assert result.closure.origin == InvoiceClosureOrigin.manual
    assert result.closure.amount == Decimal("120.00")
    assert result.closure.payments_applied == Decimal("0.00")
    assert result.closure.credits_applied == Decimal("0.00")
    assert len(result.closure.ledger_evidence) == 1
    evidence = result.closure.ledger_evidence[0]
    entry = db_session.get(LedgerEntry, evidence.ledger_entry_id)
    assert evidence.reverses_ledger_entry_id is None
    assert entry is not None
    assert entry.entry_type == LedgerEntryType.credit
    assert entry.source == LedgerSource.adjustment
    assert entry.amount == Decimal("120.00")
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "write_off")
        .filter(AuditEvent.entity_id == str(invoice.id))
        .count()
        == 1
    )


def test_void_reverses_invoice_debit_append_only_with_exact_links(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, amount="75.00")
    original = billing_service.ledger_entries.create(
        db_session,
        LedgerEntryCreate(
            account_id=subscriber.id,
            invoice_id=invoice.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.invoice,
            amount=Decimal("75.00"),
            currency="NGN",
            memo="Posted invoice charge",
        ),
    )
    preview = billing_service.invoices.preview_void(db_session, str(invoice.id))

    result = billing_service.invoices.confirm_void(
        db_session,
        str(invoice.id),
        InvoiceClosureConfirm(
            preview_fingerprint=preview.fingerprint,
            idempotency_key="invoice-void-evidence-000001",
            memo="Invoice should not have existed",
        ),
    )

    db_session.refresh(original)
    assert original.is_active is True
    assert result.invoice.status == InvoiceStatus.void
    assert result.invoice.balance_due == Decimal("0.00")
    assert len(result.closure.ledger_evidence) == 1
    evidence = result.closure.ledger_evidence[0]
    reversal = db_session.get(LedgerEntry, evidence.ledger_entry_id)
    assert evidence.reverses_ledger_entry_id == original.id
    assert reversal is not None
    assert reversal.reversal_of_entry_id == original.id
    assert reversal.entry_type == LedgerEntryType.credit
    assert reversal.source == LedgerSource.invoice
    assert reversal.amount == original.amount


def test_void_rejects_effective_payment_allocation(db_session, subscriber):
    invoice = _invoice(db_session, subscriber, amount="100.00")
    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("25.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(invoice_id=invoice.id, amount=Decimal("25.00"))
            ],
        ),
    )

    with pytest.raises(HTTPException) as exc:
        billing_service.invoices.preview_void(db_session, str(invoice.id))

    assert exc.value.status_code == 409
    assert "applied payment or credit" in str(exc.value.detail)
    assert db_session.query(InvoiceClosure).count() == 0


def test_writeoff_closes_only_remaining_receivable_after_payment(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, amount="100.00")
    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("25.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(invoice_id=invoice.id, amount=Decimal("25.00"))
            ],
        ),
    )
    preview = billing_service.invoices.preview_write_off(db_session, str(invoice.id))
    position_before = calculate_customer_balance(db_session, subscriber.id)

    assert preview.payments_applied == Decimal("25.00")
    assert preview.credits_applied == Decimal("0.00")
    assert preview.receivable_before == Decimal("75.00")
    assert preview.closure_amount == Decimal("75.00")
    result = billing_service.invoices.confirm_write_off(
        db_session,
        str(invoice.id),
        InvoiceClosureConfirm(
            preview_fingerprint=preview.fingerprint,
            idempotency_key="partial-invoice-writeoff-0001",
        ),
    )
    entry = db_session.get(
        LedgerEntry, result.closure.ledger_evidence[0].ledger_entry_id
    )
    assert entry is not None
    assert entry.amount == Decimal("75.00")
    assert position_before == Decimal("-75.00")
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("0.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber.id])[
        subscriber.id
    ]["NGN"] == Decimal("0.00")


def test_writeoff_rejects_prepaid_non_receivable(db_session, subscriber, subscription):
    subscription.billing_mode = BillingMode.prepaid
    invoice = _invoice(db_session, subscriber, amount="90.00")
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid service period",
            quantity=Decimal("1"),
            unit_price=Decimal("90.00"),
            amount=Decimal("90.00"),
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        billing_service.invoices.preview_write_off(db_session, str(invoice.id))

    assert exc.value.status_code == 409
    assert "not bad debt" in str(exc.value.detail)


def test_closure_confirmation_rejects_stale_receivable_preview(db_session, subscriber):
    invoice = _invoice(db_session, subscriber, amount="80.00")
    preview = billing_service.invoices.preview_write_off(db_session, str(invoice.id))
    invoice.total = Decimal("90.00")
    invoice.balance_due = Decimal("90.00")
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        billing_service.invoices.confirm_write_off(
            db_session,
            str(invoice.id),
            InvoiceClosureConfirm(
                preview_fingerprint=preview.fingerprint,
                idempotency_key="invoice-writeoff-stale-0001",
            ),
        )

    assert exc.value.status_code == 409
    assert db_session.query(InvoiceClosure).count() == 0
    assert db_session.query(LedgerEntry).count() == 0


def test_draft_void_records_no_money_closure(db_session, subscriber):
    invoice = _invoice(
        db_session, subscriber, status=InvoiceStatus.draft, amount="50.00"
    )
    preview = billing_service.invoices.preview_void(db_session, str(invoice.id))
    assert preview.receivable_before == Decimal("0.00")
    assert preview.ledger_effects == ()

    result = billing_service.invoices.confirm_void(
        db_session,
        str(invoice.id),
        InvoiceClosureConfirm(
            preview_fingerprint=preview.fingerprint,
            idempotency_key="invoice-draft-void-000001",
        ),
    )

    assert result.invoice.status == InvoiceStatus.void
    assert result.closure.amount == Decimal("0.00")
    assert result.closure.ledger_evidence == []
    assert db_session.query(LedgerEntry).count() == 0


def test_historical_writeoff_reconciliation_links_exact_entry_without_money(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, amount="60.00")
    invoice.status = InvoiceStatus.written_off
    invoice.balance_due = Decimal("0.00")
    historical_entry = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.adjustment,
        amount=Decimal("60.00"),
        currency="NGN",
        memo="Historical write-off",
    )
    db_session.add(historical_entry)
    db_session.commit()
    inspection = billing_service.invoices.inspect_closure_evidence(
        db_session, str(invoice.id)
    )
    ledger_count = db_session.query(LedgerEntry).count()

    result = billing_service.invoices.reconcile_closure_evidence(
        db_session,
        str(invoice.id),
        InvoiceClosureReconciliationRequest(
            closure_type=InvoiceClosureType.write_off,
            evidence=[
                InvoiceClosureEvidenceSelection(ledger_entry_id=historical_entry.id)
            ],
            preview_fingerprint=inspection.fingerprint,
            idempotency_key="historical-writeoff-evidence-01",
            reason="Operator reviewed exact entry",
        ),
    )

    assert result.closure.origin == InvoiceClosureOrigin.historical_reconciliation
    assert result.closure.ledger_evidence[0].ledger_entry_id == historical_entry.id
    assert db_session.query(LedgerEntry).count() == ledger_count


def test_historical_void_reconciliation_requires_exact_reversal_partition(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, amount="45.00")
    invoice.status = InvoiceStatus.void
    invoice.balance_due = Decimal("0.00")
    original = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        amount=Decimal("45.00"),
        currency="NGN",
        memo="Historical invoice debit",
        is_active=False,
    )
    reversal = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.adjustment,
        amount=Decimal("45.00"),
        currency="NGN",
        memo="Historical unlinked void reversal",
    )
    db_session.add_all([original, reversal])
    db_session.commit()
    inspection = billing_service.invoices.inspect_closure_evidence(
        db_session, str(invoice.id)
    )
    ledger_count = db_session.query(LedgerEntry).count()

    result = billing_service.invoices.reconcile_closure_evidence(
        db_session,
        str(invoice.id),
        InvoiceClosureReconciliationRequest(
            closure_type=InvoiceClosureType.void,
            evidence=[
                InvoiceClosureEvidenceSelection(
                    ledger_entry_id=reversal.id,
                    reverses_ledger_entry_id=original.id,
                )
            ],
            preview_fingerprint=inspection.fingerprint,
            idempotency_key="historical-invoice-void-evidence-01",
        ),
    )

    evidence = result.closure.ledger_evidence[0]
    assert evidence.ledger_entry_id == reversal.id
    assert evidence.reverses_ledger_entry_id == original.id
    assert db_session.query(LedgerEntry).count() == ledger_count


def test_generic_invoice_update_cannot_set_financial_state_or_balance(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber)

    with pytest.raises(HTTPException) as state_exc:
        billing_service.invoices.update(
            db_session,
            str(invoice.id),
            InvoiceUpdate(status=InvoiceStatus.written_off),
        )
    with pytest.raises(HTTPException) as balance_exc:
        billing_service.invoices.update(
            db_session,
            str(invoice.id),
            InvoiceUpdate(balance_due=Decimal("1.00")),
        )

    assert state_exc.value.status_code == 409
    assert balance_exc.value.status_code == 409
