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
    PaymentProviderEventFinancialEffect,
    PaymentProviderType,
    PaymentRefund,
    PaymentReversal,
    PaymentReversalOrigin,
    PaymentStatus,
)
from app.schemas.billing import (
    LedgerEntryCreate,
    PaymentAllocationCreate,
    PaymentCreate,
    PaymentProviderEventIngest,
    PaymentRefundPreviewRequest,
    PaymentRefundRequest,
    PaymentReversalPreviewRequest,
    PaymentReversalRequest,
    PaymentUpdate,
)
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance
from app.services.customer_financial_ledger import calculate_customer_balance


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


def _confirm_reversal(
    db,
    payment,
    *,
    reason: str = "confirmed bank reversal",
    key: str = "reversal-confirm-00000001",
):
    request = PaymentReversalPreviewRequest(reason=reason)
    preview = billing_service.reversals.preview(db, str(payment.id), request)
    result = billing_service.reversals.process_with_evidence(
        db,
        str(payment.id),
        PaymentReversalRequest(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=key,
        ),
    )
    return preview, result


def _confirm_refund(db, payment, amount: str):
    request = PaymentRefundPreviewRequest(
        amount=Decimal(amount), reason="confirmed refund"
    )
    preview = billing_service.refunds.preview(db, str(payment.id), request)
    return billing_service.refunds.process_with_evidence(
        db,
        str(payment.id),
        PaymentRefundRequest(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=f"refund-before-reversal-{uuid4().hex}",
        ),
    )


def test_unallocated_reversal_links_exact_ledger_audit_and_replays_once(
    db_session, subscriber
):
    payment = _payment(db_session, subscriber)
    before = calculate_customer_balance(db_session, subscriber.id)

    preview, result = _confirm_reversal(db_session, payment)
    replay = billing_service.reversals.process_with_evidence(
        db_session,
        str(payment.id),
        PaymentReversalRequest(
            reason="confirmed bank reversal",
            preview_fingerprint=preview.fingerprint,
            idempotency_key="reversal-confirm-00000001",
        ),
    )

    assert preview.prepaid_funding_before == before
    assert preview.prepaid_funding_after == before - Decimal("100.00")
    assert preview.account_credit_before == Decimal("100.00")
    assert preview.account_credit_after == Decimal("0.00")
    assert preview.account_credit_consumption == Decimal("100.00")
    assert replay.idempotent_replay is True
    assert replay.reversal.id == result.reversal.id
    assert result.reversal.ledger_entry_id == result.ledger_entry.id
    assert result.ledger_entry.payment_id == payment.id
    assert result.ledger_entry.source == LedgerSource.payment
    assert result.ledger_entry.entry_type == LedgerEntryType.debit
    assert result.ledger_entry.amount == Decimal("100.00")
    assert result.credit_consumption_ledger_entry is not None
    assert result.credit_consumption_ledger_entry.source == LedgerSource.other
    assert db_session.query(PaymentReversal).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("0.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.reversed
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "reverse")
        .filter(AuditEvent.entity_id == str(payment.id))
        .one()
    )
    assert audit.metadata_["ledger_entry_id"] == str(result.ledger_entry.id)


def test_allocated_reversal_reopens_receivable_without_wallet_debit(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber)
    payment = _payment(db_session, subscriber, invoice=invoice)
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid

    preview, result = _confirm_reversal(
        db_session,
        payment,
        key="reversal-confirm-allocated-01",
    )
    db_session.refresh(invoice)

    assert preview.invoice_effects[0].receivable_before == Decimal("0.00")
    assert preview.invoice_effects[0].receivable_after == Decimal("100.00")
    assert preview.account_credit_consumption == Decimal("0.00")
    assert result.credit_consumption_ledger_entry is None
    assert invoice.balance_due == Decimal("100.00")
    assert invoice.status != InvoiceStatus.paid
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_reversal_after_partial_refund_removes_only_remaining_settled_value(
    db_session, subscriber
):
    payment = _payment(db_session, subscriber)
    _confirm_refund(db_session, payment, "30.00")
    before = calculate_customer_balance(db_session, subscriber.id)

    preview, result = _confirm_reversal(
        db_session,
        payment,
        key="reversal-after-refund-0001",
    )

    assert preview.payment_gross == Decimal("100.00")
    assert preview.refunded_before == Decimal("30.00")
    assert preview.payment_net_before == Decimal("70.00")
    assert preview.reversal_amount == Decimal("70.00")
    assert result.ledger_entry.amount == Decimal("70.00")
    assert db_session.query(PaymentRefund).one().amount == Decimal("30.00")
    assert calculate_customer_balance(db_session, subscriber.id) == before - Decimal(
        "70.00"
    )
    db_session.refresh(payment)
    assert payment.refunded_amount == Decimal("30.00")
    assert payment.status == PaymentStatus.reversed


def test_provider_reversal_requires_normalized_exact_event(db_session, subscriber):
    provider = PaymentProvider(
        name=f"Provider {uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db_session.add(provider)
    db_session.commit()
    payment = _payment(db_session, subscriber, provider=provider)

    with pytest.raises(HTTPException) as manual:
        billing_service.reversals.preview(
            db_session,
            str(payment.id),
            PaymentReversalPreviewRequest(reason="manual chargeback"),
        )
    assert manual.value.status_code == 409

    untrusted_payload = PaymentProviderEventIngest(
        provider_id=provider.id,
        payment_id=payment.id,
        event_type="charge.reversed",
        amount=Decimal("100.00"),
        currency="NGN",
        idempotency_key=f"provider-reversal-untrusted-{uuid4().hex}",
    )
    with pytest.raises(HTTPException) as untrusted:
        billing_service.payment_provider_events.ingest(db_session, untrusted_payload)
    assert untrusted.value.status_code == 409

    event = billing_service.payment_provider_events.ingest(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="charge.reversed",
            amount=Decimal("100.00"),
            currency="NGN",
            idempotency_key=f"provider-reversal-{uuid4().hex}",
        ),
        trusted_financial_effects=True,
    )
    reversal = db_session.query(PaymentReversal).one()
    db_session.refresh(payment)

    assert event.financial_effect == (
        PaymentProviderEventFinancialEffect.reversal_confirmed
    )
    assert reversal.origin == PaymentReversalOrigin.provider_event
    assert reversal.provider_event_id == event.id
    assert reversal.amount == Decimal("100.00")
    assert payment.status == PaymentStatus.reversed


@pytest.mark.parametrize(
    ("amount", "currency"),
    [(Decimal("90.00"), "NGN"), (Decimal("100.00"), "USD")],
)
def test_provider_reversal_fails_closed_on_inexact_money(
    db_session, subscriber, amount, currency
):
    provider = PaymentProvider(
        name=f"Provider {uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db_session.add(provider)
    db_session.commit()
    payment = _payment(db_session, subscriber, provider=provider)

    with pytest.raises(HTTPException) as mismatch:
        billing_service.payment_provider_events.ingest(
            db_session,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                payment_id=payment.id,
                event_type="charge.reversed",
                amount=amount,
                currency=currency,
                idempotency_key=f"provider-reversal-bad-{uuid4().hex}",
            ),
            trusted_financial_effects=True,
        )
    assert mismatch.value.status_code == 409
    db_session.rollback()
    assert db_session.query(PaymentReversal).count() == 0


def test_provider_status_hint_without_normalized_reversal_effect_fails_closed(
    db_session, subscriber
):
    provider = PaymentProvider(
        name=f"Provider {uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db_session.add(provider)
    db_session.commit()
    payment = _payment(db_session, subscriber, provider=provider)

    with pytest.raises(HTTPException) as missing_effect:
        billing_service.payment_provider_events.ingest(
            db_session,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                payment_id=payment.id,
                event_type="provider.dispute",
                status_hint=PaymentStatus.reversed,
                amount=Decimal("100.00"),
                currency="NGN",
                idempotency_key=f"provider-reversal-unknown-{uuid4().hex}",
            ),
            trusted_financial_effects=True,
        )
    assert missing_effect.value.status_code == 409
    db_session.rollback()


def test_stale_preview_and_direct_reversed_status_are_rejected(db_session, subscriber):
    payment = _payment(db_session, subscriber)
    request = PaymentReversalPreviewRequest(reason="confirmed bank reversal")
    stale = billing_service.reversals.preview(db_session, str(payment.id), request)
    _confirm_refund(db_session, payment, "10.00")

    with pytest.raises(HTTPException) as changed:
        billing_service.reversals.process_with_evidence(
            db_session,
            str(payment.id),
            PaymentReversalRequest(
                **request.model_dump(),
                preview_fingerprint=stale.fingerprint,
                idempotency_key="reversal-stale-preview-001",
            ),
        )
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as direct:
        billing_service.payments.mark_status(
            db_session, str(payment.id), PaymentStatus.reversed
        )
    assert direct.value.status_code == 409


def test_reversal_evidence_blocks_generic_payment_and_allocation_mutation(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber)
    payment = _payment(db_session, subscriber, invoice=invoice)
    allocation = payment.allocations[0]
    _confirm_reversal(
        db_session,
        payment,
        key="reversal-immutable-evidence-01",
    )

    with pytest.raises(HTTPException) as update:
        billing_service.payments.update(
            db_session,
            str(payment.id),
            PaymentUpdate(amount=Decimal("101.00")),
        )
    assert update.value.status_code == 409
    with pytest.raises(HTTPException) as delete:
        billing_service.payments.delete(db_session, str(payment.id))
    assert delete.value.status_code == 409
    with pytest.raises(HTTPException) as allocate:
        billing_service.payment_allocations.create(
            db_session,
            PaymentAllocationCreate(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=Decimal("1.00"),
            ),
        )
    assert allocate.value.status_code == 409
    with pytest.raises(HTTPException) as deallocate:
        billing_service.payment_allocations.delete(db_session, str(allocation.id))
    assert deallocate.value.status_code == 409


def test_historical_reversal_reconciliation_requires_selected_exact_evidence(
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
            amount=Decimal("100.00"),
            currency="NGN",
            memo="Legacy chargeback",
        ),
    )
    payment.status = PaymentStatus.failed
    db_session.commit()

    inspection = billing_service.reversals.inspect_evidence(db_session, str(payment.id))
    assert inspection.unlinked_candidate_ledger_entry_ids == (legacy_entry.id,)
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "100.00"
    )

    linked = billing_service.reversals.reconcile_evidence(
        db_session,
        str(payment.id),
        reversal_ledger_entry_id=legacy_entry.id,
        account_credit_consumption=Decimal("100.00"),
    )
    replay = billing_service.reversals.reconcile_evidence(
        db_session,
        str(payment.id),
        reversal_ledger_entry_id=legacy_entry.id,
        account_credit_consumption=Decimal("100.00"),
    )

    assert replay.id == linked.id
    assert linked.preview_fingerprint is None
    assert db_session.query(PaymentReversal).count() == 1
    assert (
        db_session.query(LedgerEntry)
        .filter(
            LedgerEntry.memo.startswith("Payment reversal account-credit consumption:")
        )
        .count()
        == 1
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.reversed
