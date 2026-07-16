from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditActorType, AuditEvent
from app.models.billing import (
    BillingAccountLedgerEntry,
    ConsolidatedPaymentReturnAllocationEvidence,
    ConsolidatedPaymentReturnDocumentReconstructionEvidence,
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
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountPaymentPreviewRequest,
    BillingAccountPaymentReturnDocumentReconstructionConfirm,
    BillingAccountPaymentReturnDocumentReconstructionRequest,
)
from app.services import billing as billing_service


def _missing_return_document(
    db_session,
    *,
    return_type: str = "refund",
    return_amount: str = "125.00",
):
    reseller = Reseller(name=f"Missing return {uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Missing",
        last_name="Return",
        email=f"missing-return-{uuid.uuid4().hex[:8]}@example.com",
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
        idempotency_key=f"missing-return-source-{uuid.uuid4()}",
        origin=PaymentSettlementOrigin.system,
    ).payment
    allocation = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .one()
    )
    amount = Decimal(return_amount)
    source = LedgerSource.refund if return_type == "refund" else LedgerSource.payment
    subscriber_debit = None
    billing_debit = None
    if amount == Decimal("125.00"):
        allocation.is_active = False
        invoice.status = InvoiceStatus.issued
        invoice.balance_due = Decimal("100.00")
        subscriber_debit = LedgerEntry(
            account_id=subscriber.id,
            invoice_id=invoice.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=source,
            amount=Decimal("100.00"),
            currency="NGN",
            memo="Historical returned allocation without document",
        )
        billing_debit = BillingAccountLedgerEntry(
            billing_account_id=account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=source,
            amount=Decimal("25.00"),
            currency="NGN",
            balance_after=Decimal("0.00"),
            memo="Historical returned credit without document",
        )
        account.balance = Decimal("0.00")
    else:
        billing_debit = BillingAccountLedgerEntry(
            billing_account_id=account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=source,
            amount=amount,
            currency="NGN",
            balance_after=Decimal("25.00") - amount,
            memo="Historical partial returned credit without document",
        )
        account.balance = Decimal("25.00") - amount
    payment.status = (
        PaymentStatus.reversed
        if return_type == "reversal"
        else (
            PaymentStatus.refunded
            if amount == Decimal("125.00")
            else PaymentStatus.partially_refunded
        )
    )
    payment.refunded_amount = Decimal("0.00")
    db_session.add(billing_debit)
    if subscriber_debit is not None:
        db_session.add(subscriber_debit)
    db_session.commit()
    return (
        account,
        invoice,
        payment,
        allocation,
        subscriber_debit,
        billing_debit,
    )


def _request(
    allocation, subscriber_debit, billing_debit, *, amount="125.00", event=None
):
    allocations = (
        {allocation.id: subscriber_debit.id} if subscriber_debit is not None else {}
    )
    return BillingAccountPaymentReturnDocumentReconstructionRequest(
        billing_account_ledger_entry_id=billing_debit.id,
        allocation_ledger_entry_ids=allocations,
        provider_event_id=event.id if event is not None else None,
        reason="Reviewed missing historical consolidated return document",
        return_amount=Decimal(amount),
        source_reference=f"bank-return-{uuid.uuid4().hex}",
    )


def _command(request, preview, key):
    return BillingAccountPaymentReturnDocumentReconstructionConfirm(
        **request.model_dump(),
        proposed_return_id=preview.proposed_return_id,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key,
    )


def test_missing_document_inspection_is_read_only(db_session):
    account, _invoice, payment, allocation, subscriber_debit, billing_debit = (
        _missing_return_document(db_session)
    )
    ledger_count = db_session.query(LedgerEntry).count()
    billing_count = db_session.query(BillingAccountLedgerEntry).count()

    inspection = billing_service.consolidated_payment_return_reconciliations.inspect_missing_document_evidence(
        db_session, str(payment.id), "refund"
    )

    assert inspection.payment_state == PaymentStatus.refunded
    assert inspection.status_only_candidate is True
    assert inspection.existing_refund_ids == []
    assert inspection.existing_reversal_id is None
    assert inspection.projection_drift == Decimal("0.00")
    assert [
        item.billing_account_ledger_entry_id
        for item in inspection.billing_account_candidate_entries
    ] == [billing_debit.id]
    assert inspection.allocation_candidates[0].payment_allocation_id == allocation.id
    assert inspection.allocation_candidates[0].candidate_ledger_entry_ids == [
        subscriber_debit.id
    ]
    assert db_session.query(PaymentRefund).count() == 0
    assert db_session.query(LedgerEntry).count() == ledger_count
    assert db_session.query(BillingAccountLedgerEntry).count() == billing_count
    db_session.refresh(account)
    assert account.balance == Decimal("0.00")


@pytest.mark.parametrize(
    ("return_type", "expected_state", "expected_refunded", "document_model"),
    [
        ("refund", PaymentStatus.refunded, Decimal("125.00"), PaymentRefund),
        ("reversal", PaymentStatus.reversed, Decimal("0.00"), PaymentReversal),
    ],
)
def test_preview_reconstruct_and_replay_compose_exact_evidence_owner(
    db_session,
    return_type,
    expected_state,
    expected_refunded,
    document_model,
):
    account, invoice, payment, allocation, subscriber_debit, billing_debit = (
        _missing_return_document(db_session, return_type=return_type)
    )
    request = _request(allocation, subscriber_debit, billing_debit)
    preview = billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
        db_session,
        str(payment.id),
        return_type,
        request,
    )
    ledger_count = db_session.query(LedgerEntry).count()
    billing_count = db_session.query(BillingAccountLedgerEntry).count()
    command = _command(
        request,
        preview,
        f"missing-{return_type}-document-reconstruction",
    )

    result = billing_service.consolidated_payment_return_reconciliations.reconstruct_missing_document(
        db_session,
        str(payment.id),
        return_type,
        command,
        actor_type=AuditActorType.api_key,
        actor_id="historical-return-reviewer",
    )
    replay = billing_service.consolidated_payment_return_reconciliations.reconstruct_missing_document(
        db_session,
        str(payment.id),
        return_type,
        command,
        actor_type=AuditActorType.api_key,
        actor_id="historical-return-reviewer",
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    db_session.refresh(payment)
    db_session.refresh(allocation)
    assert preview.return_document_created is False
    assert preview.money_posted is False
    assert preview.billing_account_balance_changed is False
    assert preview.payment_state_before == expected_state
    assert preview.payment_state_after == expected_state
    assert preview.billing_account_return_amount == Decimal("25.00")
    assert preview.allocation_return_amount == Decimal("100.00")
    assert result.return_id == preview.proposed_return_id
    assert result.return_document_created is True
    assert result.payment_state == expected_state
    assert result.source_reference == request.source_reference
    assert result.billing_account_ledger_entry_id == billing_debit.id
    assert result.subscriber_ledger_entry_ids == [subscriber_debit.id]
    assert replay.reconstruction_evidence_id == result.reconstruction_evidence_id
    assert replay.idempotent_replay is True
    assert db_session.query(document_model).count() == 1
    assert db_session.query(ConsolidatedPaymentReturnAllocationEvidence).count() == 1
    assert (
        db_session.query(ConsolidatedPaymentReturnReconciliationEvidence).count() == 1
    )
    assert (
        db_session.query(
            ConsolidatedPaymentReturnDocumentReconstructionEvidence
        ).count()
        == 1
    )
    assert db_session.query(LedgerEntry).count() == ledger_count
    assert db_session.query(BillingAccountLedgerEntry).count() == billing_count
    assert account.balance == Decimal("0.00")
    assert invoice.status == InvoiceStatus.issued
    assert invoice.balance_due == Decimal("100.00")
    assert allocation.is_active is False
    assert payment.status == expected_state
    assert payment.refunded_amount == expected_refunded
    audits = {
        item.action: item
        for item in db_session.query(AuditEvent)
        .filter(
            AuditEvent.action.in_(
                [
                    "reconcile_consolidated_return_evidence",
                    "reconstruct_consolidated_return_document",
                ]
            )
        )
        .all()
    }
    assert set(audits) == {
        "reconcile_consolidated_return_evidence",
        "reconstruct_consolidated_return_document",
    }
    reconstruction_audit = audits["reconstruct_consolidated_return_document"]
    assert reconstruction_audit.actor_type == AuditActorType.api_key
    assert reconstruction_audit.actor_id == "historical-return-reviewer"
    assert reconstruction_audit.metadata_["money_posted"] is False
    assert reconstruction_audit.metadata_["billing_account_balance_changed"] is False


def test_partial_refund_document_uses_only_selected_consolidated_credit(db_session):
    account, _invoice, payment, allocation, _subscriber_debit, billing_debit = (
        _missing_return_document(db_session, return_amount="10.00")
    )
    request = _request(
        allocation,
        None,
        billing_debit,
        amount="10.00",
    )
    preview = billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
        db_session, str(payment.id), "refund", request
    )

    result = billing_service.consolidated_payment_return_reconciliations.reconstruct_missing_document(
        db_session,
        str(payment.id),
        "refund",
        _command(request, preview, "missing-partial-refund-document"),
    )

    db_session.refresh(account)
    db_session.refresh(payment)
    db_session.refresh(allocation)
    assert preview.return_amount == Decimal("10.00")
    assert preview.allocation_return_amount == Decimal("0.00")
    assert result.payment_state == PaymentStatus.partially_refunded
    assert result.subscriber_ledger_entry_ids == []
    assert payment.refunded_amount == Decimal("10.00")
    assert account.balance == Decimal("15.00")
    assert allocation.is_active is True


def test_reconstruction_rejects_status_or_evidence_that_does_not_explain_state(
    db_session,
):
    _account, _invoice, payment, allocation, _subscriber_debit, billing_debit = (
        _missing_return_document(db_session, return_amount="10.00")
    )
    request = _request(allocation, None, billing_debit, amount="10.00")
    payment.status = PaymentStatus.refunded
    db_session.commit()

    with pytest.raises(HTTPException, match="does not explain"):
        billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
            db_session, str(payment.id), "refund", request
        )

    payment.status = PaymentStatus.succeeded
    db_session.commit()
    with pytest.raises(HTTPException, match="not consistent"):
        billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
            db_session, str(payment.id), "refund", request
        )
    assert db_session.query(PaymentRefund).count() == 0


def test_reconstruction_requires_existing_return_documents_to_be_reconciled(
    db_session,
):
    _account, _invoice, payment, allocation, subscriber_debit, billing_debit = (
        _missing_return_document(db_session)
    )
    db_session.add(
        PaymentRefund(
            payment_id=payment.id,
            amount=Decimal("1.00"),
            currency="NGN",
            origin=PaymentRefundOrigin.manual,
            reason="Unreviewed historical refund document",
        )
    )
    db_session.commit()
    request = _request(allocation, subscriber_debit, billing_debit)

    with pytest.raises(HTTPException, match="must be reconciled"):
        billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
            db_session, str(payment.id), "refund", request
        )

    assert (
        db_session.query(
            ConsolidatedPaymentReturnDocumentReconstructionEvidence
        ).count()
        == 0
    )


def test_provider_backed_reconstruction_requires_exact_processed_event(db_session):
    _account, _invoice, payment, allocation, subscriber_debit, billing_debit = (
        _missing_return_document(db_session)
    )
    provider = PaymentProvider(
        name=f"Missing return provider {uuid.uuid4().hex}",
        provider_type=PaymentProviderType.custom,
    )
    db_session.add(provider)
    db_session.flush()
    payment.provider_id = provider.id
    db_session.commit()
    request = _request(allocation, subscriber_debit, billing_debit)
    with pytest.raises(HTTPException, match="evidence is required"):
        billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
            db_session, str(payment.id), "refund", request
        )

    event = PaymentProviderEvent(
        provider_id=provider.id,
        payment_id=payment.id,
        event_type="refund.completed",
        idempotency_key=f"missing-return-{uuid.uuid4().hex}",
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
        event=event,
    )

    preview = billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
        db_session, str(payment.id), "refund", exact_request
    )
    result = billing_service.consolidated_payment_return_reconciliations.reconstruct_missing_document(
        db_session,
        str(payment.id),
        "refund",
        _command(
            exact_request,
            preview,
            "missing-provider-refund-document",
        ),
    )

    refund = db_session.get(PaymentRefund, result.return_id)
    assert refund is not None
    assert refund.provider_event_id == event.id


def test_confirmation_rejects_stale_document_evidence(db_session):
    account, _invoice, payment, allocation, subscriber_debit, billing_debit = (
        _missing_return_document(db_session)
    )
    request = _request(allocation, subscriber_debit, billing_debit)
    preview = billing_service.consolidated_payment_return_reconciliations.preview_document_reconstruction(
        db_session, str(payment.id), "refund", request
    )
    account.balance = Decimal("1.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="changed after preview"):
        billing_service.consolidated_payment_return_reconciliations.reconstruct_missing_document(
            db_session,
            str(payment.id),
            "refund",
            _command(request, preview, "stale-missing-return-document"),
        )
    assert db_session.query(PaymentRefund).count() == 0
    assert (
        db_session.query(
            ConsolidatedPaymentReturnDocumentReconstructionEvidence
        ).count()
        == 0
    )
