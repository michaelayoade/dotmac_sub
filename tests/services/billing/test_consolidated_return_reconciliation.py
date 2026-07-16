from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditActorType, AuditEvent
from app.models.billing import (
    BillingAccountLedgerEntry,
    ConsolidatedPaymentReturnAllocationEvidence,
    ConsolidatedPaymentReturnReconciliationEvidence,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventStatus,
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
    BillingAccountPaymentReturnReconciliationConfirm,
    BillingAccountPaymentReturnReconciliationRequest,
)
from app.services import billing as billing_service


def _historical_return(db_session, *, return_type: str = "refund"):
    reseller = Reseller(name=f"Historical return {uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Historical",
        last_name="Return",
        email=f"historical-return-{uuid.uuid4().hex[:8]}@example.com",
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
    payment = billing_service.consolidated_payment_settlements.settle_verified(
        db_session,
        str(account.id),
        BillingAccountPaymentPreviewRequest(
            amount=Decimal("125.00"),
            currency="NGN",
        ),
        idempotency_key=f"historical-return-source-{uuid.uuid4()}",
        origin=PaymentSettlementOrigin.system,
    ).payment
    allocation = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .one()
    )
    allocation.is_active = False
    invoice.status = InvoiceStatus.issued
    invoice.balance_due = Decimal("100.00")
    account.balance = Decimal("0.00")
    source = LedgerSource.refund if return_type == "refund" else LedgerSource.payment
    subscriber_debit = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=source,
        amount=Decimal("100.00"),
        currency="NGN",
        memo="Historical returned allocation",
    )
    billing_debit = BillingAccountLedgerEntry(
        billing_account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=source,
        amount=Decimal("25.00"),
        currency="NGN",
        balance_after=Decimal("0.00"),
        memo="Historical returned consolidated credit",
    )
    if return_type == "refund":
        record = PaymentRefund(
            payment_id=payment.id,
            amount=Decimal("125.00"),
            currency="NGN",
            origin=PaymentRefundOrigin.manual,
            reason="Historical completed refund",
        )
    else:
        record = PaymentReversal(
            payment_id=payment.id,
            amount=Decimal("125.00"),
            currency="NGN",
            origin=PaymentReversalOrigin.manual,
            reason="Historical completed reversal",
        )
    db_session.add_all([subscriber_debit, billing_debit, record])
    db_session.commit()
    return (
        account,
        invoice,
        payment,
        allocation,
        subscriber_debit,
        billing_debit,
        record,
    )


def _request(allocation, subscriber_debit, billing_debit, *, provider_event=None):
    return BillingAccountPaymentReturnReconciliationRequest(
        billing_account_ledger_entry_id=billing_debit.id,
        allocation_ledger_entry_ids={allocation.id: subscriber_debit.id},
        provider_event_id=provider_event.id if provider_event is not None else None,
        reason="Reviewed exact historical consolidated return evidence",
    )


def _command(request, fingerprint: str, key: str):
    return BillingAccountPaymentReturnReconciliationConfirm(
        **request.model_dump(),
        preview_fingerprint=fingerprint,
        idempotency_key=key,
    )


def test_inspection_is_read_only_and_separates_return_evidence(db_session):
    account, _invoice, payment, allocation, subscriber_debit, billing_debit, record = (
        _historical_return(db_session)
    )
    ledger_count = db_session.query(LedgerEntry).count()
    billing_count = db_session.query(BillingAccountLedgerEntry).count()

    inspection = (
        billing_service.consolidated_payment_return_reconciliations.inspect_evidence(
            db_session,
            str(payment.id),
            "refund",
            str(record.id),
        )
    )

    assert inspection.return_amount == Decimal("125.00")
    assert inspection.recorded_consolidated_credit == Decimal("0.00")
    assert inspection.evidenced_consolidated_credit == Decimal("0.00")
    assert inspection.projection_drift == Decimal("0.00")
    assert inspection.already_reconciled is False
    assert [
        item.billing_account_ledger_entry_id
        for item in inspection.billing_account_candidate_entries
    ] == [billing_debit.id]
    assert inspection.allocation_candidates[0].payment_allocation_id == allocation.id
    assert inspection.allocation_candidates[0].allocation_active is False
    assert inspection.allocation_candidates[0].candidate_ledger_entry_ids == [
        subscriber_debit.id
    ]
    assert "no_access_decision" in inspection.service_access_consequence
    assert db_session.query(LedgerEntry).count() == ledger_count
    assert db_session.query(BillingAccountLedgerEntry).count() == billing_count
    assert (
        db_session.query(ConsolidatedPaymentReturnReconciliationEvidence).count() == 0
    )
    db_session.refresh(account)
    assert account.balance == Decimal("0.00")


@pytest.mark.parametrize(
    ("return_type", "expected_state", "expected_refunded"),
    [
        ("refund", PaymentStatus.refunded, Decimal("125.00")),
        ("reversal", PaymentStatus.reversed, Decimal("0.00")),
    ],
)
def test_preview_confirm_and_replay_link_only_exact_historical_evidence(
    db_session,
    return_type,
    expected_state,
    expected_refunded,
):
    account, invoice, payment, allocation, subscriber_debit, billing_debit, record = (
        _historical_return(db_session, return_type=return_type)
    )
    request = _request(allocation, subscriber_debit, billing_debit)
    preview = billing_service.consolidated_payment_return_reconciliations.preview(
        db_session,
        str(payment.id),
        return_type,
        str(record.id),
        request,
    )
    ledger_count = db_session.query(LedgerEntry).count()
    billing_count = db_session.query(BillingAccountLedgerEntry).count()
    command = _command(
        request,
        preview.fingerprint,
        f"historical-{return_type}-reconciliation",
    )

    result = billing_service.consolidated_payment_return_reconciliations.reconcile_historical_evidence(
        db_session,
        str(payment.id),
        return_type,
        str(record.id),
        command,
        actor_type=AuditActorType.api_key,
        actor_id="billing-reconciliation-tool",
    )
    replay = billing_service.consolidated_payment_return_reconciliations.reconcile_historical_evidence(
        db_session,
        str(payment.id),
        return_type,
        str(record.id),
        command,
        actor_type=AuditActorType.api_key,
        actor_id="billing-reconciliation-tool",
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    db_session.refresh(payment)
    db_session.refresh(allocation)
    db_session.refresh(record)
    assert preview.billing_account_return_amount == Decimal("25.00")
    assert preview.allocation_return_amount == Decimal("100.00")
    assert preview.payment_state_after == expected_state
    assert preview.money_posted is False
    assert preview.billing_account_balance_changed is False
    assert "no_access_decision" in preview.service_access_consequence
    assert result.payment_state == expected_state
    assert result.billing_account_ledger_entry_id == billing_debit.id
    assert result.subscriber_ledger_entry_ids == [subscriber_debit.id]
    assert result.money_posted is False
    assert replay.reconciliation_evidence_id == result.reconciliation_evidence_id
    assert replay.idempotent_replay is True
    assert payment.status == expected_state
    assert payment.refunded_amount == expected_refunded
    assert account.balance == Decimal("0.00")
    assert invoice.status == InvoiceStatus.issued
    assert invoice.balance_due == Decimal("100.00")
    assert allocation.is_active is False
    assert db_session.query(LedgerEntry).count() == ledger_count
    assert db_session.query(BillingAccountLedgerEntry).count() == billing_count
    assert db_session.query(ConsolidatedPaymentReturnAllocationEvidence).count() == 1
    assert (
        db_session.query(ConsolidatedPaymentReturnReconciliationEvidence).count() == 1
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "reconcile_consolidated_return_evidence")
        .one()
    )
    assert audit.actor_type == AuditActorType.api_key
    assert audit.actor_id == "billing-reconciliation-tool"
    assert audit.metadata_["money_posted"] is False
    assert audit.metadata_["billing_account_balance_changed"] is False
    assert audit.metadata_["subscriber_ledger_entry_ids"] == [str(subscriber_debit.id)]


def test_reconciliation_rejects_active_or_incomplete_allocation_evidence(db_session):
    _account, _invoice, payment, allocation, subscriber_debit, billing_debit, record = (
        _historical_return(db_session)
    )
    request = _request(allocation, subscriber_debit, billing_debit)
    allocation.is_active = True
    db_session.commit()

    with pytest.raises(HTTPException, match="already-returned"):
        billing_service.consolidated_payment_return_reconciliations.preview(
            db_session, str(payment.id), "refund", str(record.id), request
        )

    allocation.is_active = False
    db_session.commit()
    incomplete = BillingAccountPaymentReturnReconciliationRequest(
        billing_account_ledger_entry_id=billing_debit.id,
        reason="Reviewed incomplete historical consolidated return evidence",
    )
    with pytest.raises(HTTPException, match="exactly partition"):
        billing_service.consolidated_payment_return_reconciliations.preview(
            db_session, str(payment.id), "refund", str(record.id), incomplete
        )


def test_reconciliation_requires_zero_projection_drift_and_fresh_preview(db_session):
    account, _invoice, payment, allocation, subscriber_debit, billing_debit, record = (
        _historical_return(db_session)
    )
    request = _request(allocation, subscriber_debit, billing_debit)
    account.balance = Decimal("1.00")
    db_session.commit()
    with pytest.raises(HTTPException, match="projection drift"):
        billing_service.consolidated_payment_return_reconciliations.preview(
            db_session, str(payment.id), "refund", str(record.id), request
        )

    account.balance = Decimal("0.00")
    db_session.commit()
    preview = billing_service.consolidated_payment_return_reconciliations.preview(
        db_session, str(payment.id), "refund", str(record.id), request
    )
    account.balance = Decimal("1.00")
    db_session.commit()
    with pytest.raises(HTTPException, match="changed after preview"):
        billing_service.consolidated_payment_return_reconciliations.reconcile_historical_evidence(
            db_session,
            str(payment.id),
            "refund",
            str(record.id),
            _command(request, preview.fingerprint, "historical-return-stale-preview"),
        )
    assert (
        db_session.query(ConsolidatedPaymentReturnReconciliationEvidence).count() == 0
    )


def test_provider_backed_return_requires_exact_processed_event(db_session):
    _account, _invoice, payment, allocation, subscriber_debit, billing_debit, record = (
        _historical_return(db_session)
    )
    provider = PaymentProvider(
        name=f"Historical return provider {uuid.uuid4().hex}",
        provider_type=PaymentProviderType.custom,
    )
    db_session.add(provider)
    db_session.flush()
    payment.provider_id = provider.id
    db_session.commit()
    request = _request(allocation, subscriber_debit, billing_debit)

    with pytest.raises(HTTPException, match="evidence is required"):
        billing_service.consolidated_payment_return_reconciliations.preview(
            db_session, str(payment.id), "refund", str(record.id), request
        )

    event = PaymentProviderEvent(
        provider_id=provider.id,
        payment_id=payment.id,
        event_type="refund.completed",
        idempotency_key=f"historical-return-{uuid.uuid4().hex}",
        amount=Decimal("125.00"),
        currency="NGN",
        financial_effect=PaymentProviderEventFinancialEffect.refund_confirmed,
        status=PaymentProviderEventStatus.processed,
    )
    db_session.add(event)
    db_session.commit()
    exact_request = _request(
        allocation,
        subscriber_debit,
        billing_debit,
        provider_event=event,
    )

    preview = billing_service.consolidated_payment_return_reconciliations.preview(
        db_session,
        str(payment.id),
        "refund",
        str(record.id),
        exact_request,
    )

    assert preview.provider_event_id == event.id
    result = billing_service.consolidated_payment_return_reconciliations.reconcile_historical_evidence(
        db_session,
        str(payment.id),
        "refund",
        str(record.id),
        _command(
            exact_request,
            preview.fingerprint,
            "historical-provider-return-reconciliation",
        ),
    )
    db_session.refresh(record)
    assert result.provider_event_id == event.id
    assert record.provider_event_id == event.id
    assert record.origin == PaymentRefundOrigin.provider_event
