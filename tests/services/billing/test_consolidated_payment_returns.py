from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    BillingAccountLedgerEntry,
    ConsolidatedPaymentReturnAllocationEvidence,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    PaymentProvider,
    PaymentProviderEventFinancialEffect,
    PaymentProviderType,
    PaymentRefund,
    PaymentRefundOrigin,
    PaymentReversal,
    PaymentReversalOrigin,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountPaymentPreviewRequest,
    BillingAccountPaymentRefundRequest,
    BillingAccountPaymentReversalRequest,
    PaymentProviderEventIngest,
    PaymentRefundPreviewRequest,
    PaymentReversalPreviewRequest,
)
from app.services import billing as billing_service


def _settled_consolidated_payment(db_session, *, provider=None):
    reseller = Reseller(name=f"Return-{uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    billing_account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Return",
        last_name="Member",
        email=f"return-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    request = BillingAccountPaymentPreviewRequest(
        amount=Decimal("125.00"),
        currency="NGN",
        provider_id=provider.id if provider else None,
    )
    payment = billing_service.consolidated_payment_settlements.settle_verified(
        db_session,
        str(billing_account.id),
        request,
        idempotency_key=f"test-consolidated-return-settle-{uuid.uuid4()}",
        origin=PaymentSettlementOrigin.system,
    ).payment
    db_session.refresh(invoice)
    db_session.refresh(billing_account)
    assert invoice.status == InvoiceStatus.paid
    assert billing_account.balance == Decimal("25.00")
    return billing_account, subscriber, invoice, payment


def test_partial_refund_consumes_only_evidenced_consolidated_credit(db_session):
    account, _subscriber, invoice, payment = _settled_consolidated_payment(db_session)
    request = PaymentRefundPreviewRequest(
        amount=Decimal("10.00"), reason="Operator-confirmed refund"
    )
    preview = billing_service.consolidated_payment_refunds.preview(
        db_session, str(payment.id), request
    )

    assert preview.consolidated_credit_before == Decimal("25.00")
    assert preview.consolidated_credit_after == Decimal("15.00")
    assert preview.consolidated_credit_consumption == Decimal("10.00")
    assert preview.invoice_effects == []
    assert preview.status_after == PaymentStatus.partially_refunded

    command = BillingAccountPaymentRefundRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key="test-consolidated-partial-refund",
    )
    result = billing_service.consolidated_payment_refunds.confirm(
        db_session, str(payment.id), command, actor_id="operator-1"
    )
    replay = billing_service.consolidated_payment_refunds.confirm(
        db_session, str(payment.id), command, actor_id="operator-1"
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    assert result.refund.ledger_entry_id is None
    assert result.billing_account_ledger_entry is not None
    assert result.billing_account_ledger_entry.entry_type == LedgerEntryType.debit
    assert result.billing_account_ledger_entry.source == LedgerSource.refund
    assert result.billing_account_ledger_entry.amount == Decimal("10.00")
    assert result.allocation_evidence == ()
    assert account.balance == Decimal("15.00")
    assert invoice.status == InvoiceStatus.paid
    assert replay.idempotent_replay is True
    assert replay.refund.id == result.refund.id
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "refund_consolidated_payment")
        .filter(AuditEvent.entity_id == str(payment.id))
        .one()
    )
    assert audit.actor_id == "operator-1"
    assert audit.metadata_["billing_account_ledger_entry_id"] == str(
        result.billing_account_ledger_entry.id
    )


def test_partial_refund_cannot_infer_an_allocation_clawback(db_session):
    _account, _subscriber, _invoice, payment = _settled_consolidated_payment(db_session)

    with pytest.raises(HTTPException, match="partial consolidated refund"):
        billing_service.consolidated_payment_refunds.preview(
            db_session,
            str(payment.id),
            PaymentRefundPreviewRequest(amount=Decimal("30.00"), reason="Too much"),
        )

    assert db_session.query(PaymentRefund).count() == 0


def test_full_refund_reopens_receivable_and_links_every_ledger_result(db_session):
    account, subscriber, invoice, payment = _settled_consolidated_payment(db_session)
    request = PaymentRefundPreviewRequest(reason="Full confirmed refund")
    preview = billing_service.consolidated_payment_refunds.preview(
        db_session, str(payment.id), request
    )

    assert preview.refund_amount == Decimal("125.00")
    assert preview.consolidated_credit_consumption == Decimal("25.00")
    assert len(preview.invoice_effects) == 1
    assert preview.invoice_effects[0].return_amount == Decimal("100.00")
    assert preview.invoice_effects[0].receivable_after == Decimal("100.00")

    result = billing_service.consolidated_payment_refunds.confirm(
        db_session,
        str(payment.id),
        BillingAccountPaymentRefundRequest(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key="test-consolidated-full-refund",
        ),
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    db_session.refresh(payment)
    assert account.balance == Decimal("0.00")
    assert payment.status == PaymentStatus.refunded
    assert invoice.status == InvoiceStatus.issued
    assert invoice.balance_due == Decimal("100.00")
    assert len(result.allocation_evidence) == 1
    evidence = result.allocation_evidence[0]
    result_entry = db_session.get(LedgerEntry, evidence.ledger_entry_id)
    assert result_entry is not None
    assert result_entry.account_id == subscriber.id
    assert result_entry.invoice_id == invoice.id
    assert result_entry.payment_id == payment.id
    assert result_entry.entry_type == LedgerEntryType.debit
    assert result_entry.source == LedgerSource.refund
    assert result_entry.amount == Decimal("100.00")
    assert result.billing_account_ledger_entry is not None
    assert result.billing_account_ledger_entry.amount == Decimal("25.00")
    assert db_session.query(ConsolidatedPaymentReturnAllocationEvidence).count() == 1


def test_reversal_returns_all_remaining_value_with_distinct_state(db_session):
    account, _subscriber, invoice, payment = _settled_consolidated_payment(db_session)
    request = PaymentReversalPreviewRequest(reason="Confirmed bank reversal")
    preview = billing_service.consolidated_payment_reversals.preview(
        db_session, str(payment.id), request
    )

    assert preview.reversal_amount == Decimal("125.00")
    assert preview.status_after == PaymentStatus.reversed
    assert preview.consolidated_credit_consumption == Decimal("25.00")
    assert len(preview.invoice_effects) == 1

    command = BillingAccountPaymentReversalRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key="test-consolidated-reversal",
    )
    result = billing_service.consolidated_payment_reversals.confirm(
        db_session,
        str(payment.id),
        command,
    )
    replay = billing_service.consolidated_payment_reversals.confirm(
        db_session,
        str(payment.id),
        command,
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    db_session.refresh(payment)
    assert account.balance == Decimal("0.00")
    assert payment.status == PaymentStatus.reversed
    assert invoice.balance_due == Decimal("100.00")
    assert result.reversal.ledger_entry_id is None
    assert result.billing_account_ledger_entry is not None
    assert result.billing_account_ledger_entry.source == LedgerSource.payment
    assert len(result.allocation_evidence) == 1
    assert replay.idempotent_replay is True
    assert replay.reversal.id == result.reversal.id
    subscriber_entry = db_session.get(
        LedgerEntry, result.allocation_evidence[0].ledger_entry_id
    )
    assert subscriber_entry is not None
    assert subscriber_entry.source == LedgerSource.payment
    assert db_session.query(PaymentReversal).count() == 1
    assert (
        db_session.query(BillingAccountLedgerEntry)
        .filter(BillingAccountLedgerEntry.payment_id == payment.id)
        .filter(BillingAccountLedgerEntry.entry_type == LedgerEntryType.debit)
        .count()
        == 1
    )


def test_return_confirmation_rejects_receivable_drift(db_session):
    _account, _subscriber, invoice, payment = _settled_consolidated_payment(db_session)
    request = PaymentReversalPreviewRequest(reason="Confirmed bank reversal")
    preview = billing_service.consolidated_payment_reversals.preview(
        db_session, str(payment.id), request
    )
    invoice.balance_due = Decimal("1.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="Financial state changed"):
        billing_service.consolidated_payment_reversals.confirm(
            db_session,
            str(payment.id),
            BillingAccountPaymentReversalRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key="test-consolidated-stale-reversal",
            ),
        )

    assert db_session.query(PaymentReversal).count() == 0


def test_trusted_provider_refund_dispatches_to_consolidated_owner(db_session):
    provider = PaymentProvider(
        name=f"Provider {uuid.uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db_session.add(provider)
    db_session.commit()
    account, _subscriber, invoice, payment = _settled_consolidated_payment(
        db_session, provider=provider
    )

    event = billing_service.payment_provider_events.ingest(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="charge.refunded",
            amount=Decimal("10.00"),
            currency="NGN",
            idempotency_key=f"provider-consolidated-refund-{uuid.uuid4().hex}",
        ),
        trusted_financial_effects=True,
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    db_session.refresh(payment)
    refund = db_session.query(PaymentRefund).one()
    assert (
        event.financial_effect == PaymentProviderEventFinancialEffect.refund_confirmed
    )
    assert refund.origin == PaymentRefundOrigin.provider_event
    assert refund.provider_event_id == event.id
    assert refund.billing_account_ledger_entry_id is not None
    assert account.balance == Decimal("15.00")
    assert invoice.status == InvoiceStatus.paid
    assert payment.status == PaymentStatus.partially_refunded


def test_trusted_provider_reversal_dispatches_to_consolidated_owner(db_session):
    provider = PaymentProvider(
        name=f"Provider {uuid.uuid4().hex}", provider_type=PaymentProviderType.custom
    )
    db_session.add(provider)
    db_session.commit()
    account, _subscriber, invoice, payment = _settled_consolidated_payment(
        db_session, provider=provider
    )

    event = billing_service.payment_provider_events.ingest(
        db_session,
        PaymentProviderEventIngest(
            provider_id=provider.id,
            payment_id=payment.id,
            event_type="charge.reversed",
            amount=Decimal("125.00"),
            currency="NGN",
            idempotency_key=f"provider-consolidated-reversal-{uuid.uuid4().hex}",
        ),
        trusted_financial_effects=True,
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    db_session.refresh(payment)
    reversal = db_session.query(PaymentReversal).one()
    assert (
        event.financial_effect == PaymentProviderEventFinancialEffect.reversal_confirmed
    )
    assert reversal.origin == PaymentReversalOrigin.provider_event
    assert reversal.provider_event_id == event.id
    assert payment.status == PaymentStatus.reversed
    assert account.balance == Decimal("0.00")
    assert invoice.balance_due == Decimal("100.00")
