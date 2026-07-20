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
    PaymentProvider,
    PaymentProviderType,
    PaymentRefund,
    PaymentRefundOrigin,
    PaymentStatus,
)
from app.schemas.billing import (
    LedgerEntryCreate,
    PaymentCreate,
    PaymentProviderEventIngest,
    PaymentRefundPreviewRequest,
    PaymentRefundRequest,
)
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance
from app.services.customer_financial_ledger import calculate_customer_balance


def _payment(db, subscriber, amount: str = "100.00", *, invoice=None, provider=None):
    return billing_service.payments.create(
        db,
        PaymentCreate(
            account_id=subscriber.id,
            invoice_id=invoice.id if invoice else None,
            provider_id=provider.id if provider else None,
            amount=Decimal(amount),
            currency="NGN",
            status=PaymentStatus.succeeded,
        ),
    )


def _invoice(db, subscriber, amount: str = "100.00") -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{uuid4().hex[:10]}",
        status=InvoiceStatus.issued,
        total=Decimal(amount),
        balance_due=Decimal(amount),
        currency="NGN",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def _confirm(db, payment, *, amount: str, key: str = "refund-confirm-00000001"):
    request = PaymentRefundPreviewRequest(amount=Decimal(amount), reason="confirmed")
    preview = billing_service.refunds.preview(db, str(payment.id), request)
    result = billing_service.refunds.process_with_evidence(
        db,
        str(payment.id),
        PaymentRefundRequest(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=key,
        ),
    )
    return preview, result


def test_refund_confirmation_links_exact_ledger_audit_and_replays_once(
    db_session, subscriber
):
    payment = _payment(db_session, subscriber)
    before = calculate_customer_balance(db_session, subscriber.id)

    preview, result = _confirm(db_session, payment, amount="30.00")
    replay = billing_service.refunds.process_with_evidence(
        db_session,
        str(payment.id),
        PaymentRefundRequest(
            amount=Decimal("30.00"),
            reason="confirmed",
            preview_fingerprint=preview.fingerprint,
            idempotency_key="refund-confirm-00000001",
        ),
    )

    assert replay.idempotent_replay is True
    assert replay.refund.id == result.refund.id
    assert result.refund.ledger_entry_id == result.ledger_entry.id
    assert result.ledger_entry.payment_id == payment.id
    assert result.ledger_entry.source == LedgerSource.refund
    assert result.ledger_entry.entry_type == LedgerEntryType.debit
    assert result.ledger_entry.amount == Decimal("30.00")
    assert result.credit_consumption_ledger_entry is not None
    assert (
        result.refund.credit_consumption_ledger_entry_id
        == result.credit_consumption_ledger_entry.id
    )
    assert db_session.query(PaymentRefund).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == before - Decimal(
        "30.00"
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "70.00"
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "refund")
        .filter(AuditEvent.entity_id == str(payment.id))
        .one()
    )
    assert audit.metadata_["ledger_entry_id"] == str(result.ledger_entry.id)


def test_allocated_refund_changes_receivable_without_debiting_account_credit(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber)
    payment = _payment(db_session, subscriber, invoice=invoice)

    preview, result = _confirm(
        db_session,
        payment,
        amount="25.00",
        key="refund-confirm-allocated-01",
    )
    db_session.refresh(invoice)

    assert preview.invoice_effects[0].receivable_after == Decimal("25.00")
    assert preview.account_credit_consumption == Decimal("0.00")
    assert result.credit_consumption_ledger_entry is None
    assert invoice.balance_due == Decimal("25.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_provider_refund_requires_normalized_event_amount(db_session, subscriber):
    provider = PaymentProvider(
        name=f"Provider {uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db_session.add(provider)
    db_session.commit()
    payment = _payment(db_session, subscriber, provider=provider)

    with pytest.raises(HTTPException) as manual:
        billing_service.refunds.preview(
            db_session,
            str(payment.id),
            PaymentRefundPreviewRequest(amount=Decimal("40.00")),
        )
    assert manual.value.status_code == 409

    event = billing_service.payment_provider_events.ingest(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="charge.refunded",
            amount=Decimal("40.00"),
            currency="NGN",
            idempotency_key=f"provider-refund-{uuid4().hex}",
        ),
        trusted_financial_effects=True,
    )
    refund = db_session.query(PaymentRefund).one()
    db_session.refresh(payment)

    assert refund.origin == PaymentRefundOrigin.provider_event
    assert refund.provider_event_id == event.id
    assert refund.amount == Decimal("40.00")
    assert payment.status == PaymentStatus.partially_refunded
    assert payment.refunded_amount == Decimal("40.00")


def test_stale_preview_and_refund_plus_credit_note_are_rejected(db_session, subscriber):
    payment = _payment(db_session, subscriber)
    stale_request = PaymentRefundPreviewRequest(amount=Decimal("30.00"))
    stale = billing_service.refunds.preview(db_session, str(payment.id), stale_request)
    _confirm(
        db_session,
        payment,
        amount="10.00",
        key="refund-confirm-stale-0001",
    )

    with pytest.raises(HTTPException) as changed:
        billing_service.refunds.process_with_evidence(
            db_session,
            str(payment.id),
            PaymentRefundRequest(
                amount=Decimal("30.00"),
                preview_fingerprint=stale.fingerprint,
                idempotency_key="refund-confirm-stale-0002",
            ),
        )
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as double_benefit:
        billing_service.refunds.process_refund(
            db_session,
            str(payment.id),
            create_credit_note=True,
            idempotency_key="refund-plus-credit-note-01",
        )
    assert double_benefit.value.status_code == 409


def test_historical_reconciliation_requires_explicit_ledger_and_is_idempotent(
    db_session, subscriber
):
    payment = _payment(db_session, subscriber)
    legacy_entry = billing_service.ledger_entries.create(
        db_session,
        LedgerEntryCreate(
            account_id=subscriber.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.refund,
            amount=Decimal("30.00"),
            currency="NGN",
            memo="Legacy refund",
        ),
    )
    payment.refunded_amount = Decimal("30.00")
    payment.status = PaymentStatus.partially_refunded
    db_session.commit()

    inspection = billing_service.refunds.inspect_evidence(db_session, str(payment.id))
    assert inspection.unlinked_ledger_entry_ids == (legacy_entry.id,)
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "100.00"
    )

    linked = billing_service.refunds.reconcile_evidence(
        db_session,
        str(payment.id),
        refund_ledger_entry_id=legacy_entry.id,
        account_credit_consumption=Decimal("30.00"),
    )
    replay = billing_service.refunds.reconcile_evidence(
        db_session,
        str(payment.id),
        refund_ledger_entry_id=legacy_entry.id,
        account_credit_consumption=Decimal("30.00"),
    )

    assert replay.id == linked.id
    assert db_session.query(PaymentRefund).count() == 1
    assert (
        db_session.query(LedgerEntry)
        .filter(
            LedgerEntry.memo.startswith("Payment refund account-credit consumption:")
        )
        .count()
        == 1
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "70.00"
    )
