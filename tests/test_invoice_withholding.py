"""Sub observes legacy evidence; it cannot adopt it as new native cash."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.payment_proof import WithholdingTaxRecord
from app.services import invoice_withholding as owner
from app.services.billing._common import resolve_invoice_settlement_amounts


def _invoice(db, subscriber):
    row = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("100000"),
        tax_total=Decimal("7500"),
        total=Decimal("107500"),
        balance_due=Decimal("107500"),
    )
    db.add(row)
    db.commit()
    return row


def test_legacy_cash_identity_cannot_be_accepted_as_native_receipt(db_session):
    with pytest.raises(owner.InvoiceWithholdingError) as caught:
        owner.preview_invoice_withholding(
            db_session, owner.InvoiceWithholdingPreviewRequest(uuid4(), uuid4())
        )
    assert caught.value.code.endswith("native_cash_adoption_required")
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_reduced_transfer_instructions_require_real_native_adoption(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber)
    with pytest.raises(owner.InvoiceWithholdingError) as caught:
        owner.snapshot_invoice_withholding_for_transfer(
            db_session, owner.InvoiceWithholdingQuery(invoice.id)
        )
    assert caught.value.code.endswith("native_transfer_adoption_required")
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0


def test_legacy_gross_record_is_displayed_without_second_discharge(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber)
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("107500"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add_all(
        [
            PaymentAllocation(
                payment_id=payment.id, invoice_id=invoice.id, amount=payment.amount
            ),
            WithholdingTaxRecord(
                account_id=subscriber.id,
                payment_id=payment.id,
                source_invoice_id=invoice.id,
                gross_amount=Decimal("107500"),
                net_amount=Decimal("102500"),
                wht_amount=Decimal("5000"),
                currency="NGN",
            ),
        ]
    )
    invoice.status = InvoiceStatus.paid
    invoice.balance_due = Decimal("0")
    db_session.commit()
    summary = owner.query_invoice_withholding(
        db_session, owner.InvoiceWithholdingQuery(invoice.id)
    )
    assert summary.legacy_gross_payment
    assert summary.accepted_cash_amount == Decimal("102500")
    assert summary.accepted_wht_amount == Decimal("5000")
    assert summary.balance_due == Decimal("0")
    assert summary.native_discharge_id is None
    assert resolve_invoice_settlement_amounts(
        db_session, invoice.id
    ).total_applied == Decimal("107500")
    assert db_session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_missing_native_policy_is_not_replaced_by_legacy_percentage(db_session):
    with pytest.raises(owner.InvoiceWithholdingError) as caught:
        owner.query_native_withholding_tax_binding(
            db_session, owner.NativeWithholdingTaxBindingQuery(owner.OPERATOR_TENANT_ID)
        )
    assert caught.value.code.endswith("native_tax_policy_required")
