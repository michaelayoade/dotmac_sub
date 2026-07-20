from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditActorType, AuditEvent
from app.models.billing import (
    BillingAccountLedgerEntry,
    ConsolidatedPaymentSettlementReconciliationEvidence,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentSettlement,
    PaymentStatus,
    TopupIntent,
)
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountPaymentSettlementReconciliationConfirm,
    BillingAccountPaymentSettlementReconciliationRequest,
)
from app.services import billing as billing_service


def _historical_payment(
    db_session,
    *,
    recorded_balance: str = "40.00",
    include_provenance: bool = True,
):
    reseller = Reseller(name=f"Historical-{uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Historical",
        last_name="Member",
        email=f"historical-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        total=Decimal("100.00"),
        balance_due=Decimal("0.00"),
    )
    payment = Payment(
        billing_account_id=account.id,
        amount=Decimal("125.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        memo="historical consolidated receipt",
    )
    db_session.add_all([invoice, payment])
    db_session.flush()
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal("100.00"),
        is_active=True,
    )
    subscriber_entry = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("100.00"),
        currency="NGN",
        memo="historical allocation result",
    )
    billing_entry = BillingAccountLedgerEntry(
        billing_account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("25.00"),
        currency="NGN",
        balance_after=Decimal("25.00"),
        memo="historical consolidated remainder",
    )
    account.balance = Decimal(recorded_balance)
    db_session.add_all([allocation, subscriber_entry, billing_entry])
    topup = None
    if include_provenance:
        topup = TopupIntent(
            billing_account_id=account.id,
            completed_payment_id=payment.id,
            reference=f"history-{uuid.uuid4().hex}",
            provider_type="manual",
            currency="NGN",
            requested_amount=Decimal("125.00"),
            actual_amount=Decimal("125.00"),
            status="completed",
        )
        db_session.add(topup)
    db_session.commit()
    return account, payment, allocation, subscriber_entry, billing_entry, topup


def _request(allocation, subscriber_entry, billing_entry, topup):
    return BillingAccountPaymentSettlementReconciliationRequest(
        allocation_ledger_entry_ids={allocation.id: subscriber_entry.id},
        billing_account_ledger_entry_id=billing_entry.id,
        provenance_type="topup_intent",
        provenance_id=topup.id,
        reason="Reviewed exact historical consolidated settlement evidence",
    )


def _command(request, fingerprint: str, key: str):
    return BillingAccountPaymentSettlementReconciliationConfirm(
        **request.model_dump(),
        preview_fingerprint=fingerprint,
        idempotency_key=key,
    )


def test_inspection_is_read_only_and_separates_projection_drift(db_session):
    account, payment, allocation, subscriber_entry, billing_entry, topup = (
        _historical_payment(db_session)
    )

    inspection = billing_service.consolidated_payment_settlements.inspect_reconciliation_evidence(
        db_session, str(payment.id)
    )

    assert inspection.payment_state == PaymentStatus.succeeded
    assert inspection.recorded_consolidated_credit == Decimal("40.00")
    assert inspection.evidenced_consolidated_credit == Decimal("25.00")
    assert inspection.projection_drift == Decimal("15.00")
    assert inspection.active_allocation_ids == [allocation.id]
    assert [
        item.ledger_entry_id for item in inspection.allocation_candidate_entries
    ] == [subscriber_entry.id]
    assert [
        item.billing_account_ledger_entry_id
        for item in inspection.billing_account_candidate_entries
    ] == [billing_entry.id]
    assert [item.provenance_id for item in inspection.provenance_candidates] == [
        topup.id
    ]
    assert db_session.query(PaymentSettlement).count() == 0
    db_session.refresh(account)
    assert account.balance == Decimal("40.00")


def test_preview_confirm_and_replay_attach_only_exact_historical_evidence(db_session):
    account, payment, allocation, subscriber_entry, billing_entry, topup = (
        _historical_payment(db_session)
    )
    request = _request(allocation, subscriber_entry, billing_entry, topup)
    preview = billing_service.consolidated_payment_settlements.preview_reconciliation(
        db_session, str(payment.id), request
    )
    subscriber_entries_before = db_session.query(LedgerEntry).count()
    billing_entries_before = db_session.query(BillingAccountLedgerEntry).count()

    command = _command(
        request,
        preview.fingerprint,
        "test-consolidated-history-evidence",
    )
    result = (
        billing_service.consolidated_payment_settlements.reconcile_historical_evidence(
            db_session,
            str(payment.id),
            command,
            actor_type=AuditActorType.api_key,
            actor_id="reconciliation-tool",
        )
    )
    replay = (
        billing_service.consolidated_payment_settlements.reconcile_historical_evidence(
            db_session,
            str(payment.id),
            command,
            actor_type=AuditActorType.api_key,
            actor_id="reconciliation-tool",
        )
    )

    db_session.refresh(account)
    db_session.refresh(allocation)
    assert preview.allocated_amount == Decimal("100.00")
    assert preview.unallocated_amount == Decimal("25.00")
    assert preview.money_posted is False
    assert preview.service_access_consequence == "none_evidence_only_no_access_decision"
    assert allocation.ledger_entry_id == subscriber_entry.id
    assert result.settlement.billing_account_ledger_entry_id == billing_entry.id
    assert result.settlement.unallocated_amount == Decimal("25.00")
    assert result.evidence.topup_intent_id == topup.id
    assert result.evidence.provider_event_id is None
    assert result.evidence.payment_proof_id is None
    assert replay.settlement.id == result.settlement.id
    assert replay.evidence.id == result.evidence.id
    assert replay.idempotent_replay is True
    assert db_session.query(LedgerEntry).count() == subscriber_entries_before
    assert db_session.query(BillingAccountLedgerEntry).count() == billing_entries_before
    assert account.balance == Decimal("40.00")
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "reconcile_consolidated_settlement_evidence")
        .one()
    )
    assert audit.actor_type == AuditActorType.api_key
    assert audit.actor_id == "reconciliation-tool"
    assert audit.metadata_["money_posted"] is False
    assert audit.metadata_["projection_drift"] == "15.00"
    assert (
        db_session.query(ConsolidatedPaymentSettlementReconciliationEvidence).count()
        == 1
    )


def test_confirmation_rejects_changed_evidence_without_creating_settlement(db_session):
    _account, payment, allocation, subscriber_entry, billing_entry, topup = (
        _historical_payment(db_session)
    )
    request = _request(allocation, subscriber_entry, billing_entry, topup)
    preview = billing_service.consolidated_payment_settlements.preview_reconciliation(
        db_session, str(payment.id), request
    )
    subscriber_entry.amount = Decimal("99.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="not an exact match"):
        billing_service.consolidated_payment_settlements.reconcile_historical_evidence(
            db_session,
            str(payment.id),
            _command(
                request,
                preview.fingerprint,
                "test-consolidated-history-stale",
            ),
        )

    assert db_session.query(PaymentSettlement).count() == 0
    assert (
        db_session.query(ConsolidatedPaymentSettlementReconciliationEvidence).count()
        == 0
    )


def test_synthesized_payment_without_cash_provenance_is_refused(db_session):
    _account, payment, allocation, subscriber_entry, billing_entry, _topup = (
        _historical_payment(db_session, include_provenance=False)
    )
    request = BillingAccountPaymentSettlementReconciliationRequest(
        allocation_ledger_entry_ids={allocation.id: subscriber_entry.id},
        billing_account_ledger_entry_id=billing_entry.id,
        provenance_type="topup_intent",
        provenance_id=uuid.uuid4(),
        reason="Reviewed historical record has no original cash provenance",
    )

    with pytest.raises(HTTPException, match="exactly one matching cash provenance"):
        billing_service.consolidated_payment_settlements.preview_reconciliation(
            db_session, str(payment.id), request
        )

    assert db_session.query(PaymentSettlement).count() == 0


def test_ambiguous_cash_provenance_is_refused(db_session):
    account, payment, allocation, subscriber_entry, billing_entry, topup = (
        _historical_payment(db_session)
    )
    proof = PaymentProof(
        billing_account_id=account.id,
        amount=Decimal("125.00"),
        gross_amount=Decimal("125.00"),
        currency="NGN",
        file_path="tests/fixtures/historical-proof.pdf",
        status=PaymentProofStatus.verified,
        payment_id=payment.id,
    )
    db_session.add(proof)
    db_session.commit()

    with pytest.raises(HTTPException, match="exactly one matching cash provenance"):
        billing_service.consolidated_payment_settlements.preview_reconciliation(
            db_session,
            str(payment.id),
            _request(allocation, subscriber_entry, billing_entry, topup),
        )

    assert db_session.query(PaymentSettlement).count() == 0


def test_verified_proof_gross_value_is_valid_cash_provenance(db_session):
    account, payment, allocation, subscriber_entry, billing_entry, _topup = (
        _historical_payment(db_session, include_provenance=False)
    )
    proof = PaymentProof(
        billing_account_id=account.id,
        amount=Decimal("120.00"),
        verified_amount=Decimal("120.00"),
        wht_amount=Decimal("5.00"),
        currency="NGN",
        file_path="tests/fixtures/historical-wht-proof.pdf",
        status=PaymentProofStatus.verified,
        payment_id=payment.id,
    )
    db_session.add(proof)
    db_session.commit()
    request = BillingAccountPaymentSettlementReconciliationRequest(
        allocation_ledger_entry_ids={allocation.id: subscriber_entry.id},
        billing_account_ledger_entry_id=billing_entry.id,
        provenance_type="payment_proof",
        provenance_id=proof.id,
        reason="Reviewed verified transfer proof and withholding tax value",
    )

    preview = billing_service.consolidated_payment_settlements.preview_reconciliation(
        db_session, str(payment.id), request
    )

    assert preview.provenance_type == "payment_proof"
    assert preview.provenance_id == proof.id
    assert preview.payment_amount == Decimal("125.00")


def test_processed_provider_event_is_valid_cash_provenance(db_session):
    _account, payment, allocation, subscriber_entry, billing_entry, _topup = (
        _historical_payment(db_session, include_provenance=False)
    )
    provider = PaymentProvider(
        name=f"Historical provider {uuid.uuid4().hex}",
        provider_type=PaymentProviderType.custom,
    )
    db_session.add(provider)
    db_session.flush()
    payment.provider_id = provider.id
    event = PaymentProviderEvent(
        provider_id=provider.id,
        payment_id=payment.id,
        event_type="payment.succeeded",
        idempotency_key=f"history-{uuid.uuid4().hex}",
        amount=Decimal("125.00"),
        currency="NGN",
        financial_effect=PaymentProviderEventFinancialEffect.none,
        status=PaymentProviderEventStatus.processed,
    )
    db_session.add(event)
    db_session.commit()
    request = BillingAccountPaymentSettlementReconciliationRequest(
        allocation_ledger_entry_ids={allocation.id: subscriber_entry.id},
        billing_account_ledger_entry_id=billing_entry.id,
        provenance_type="provider_event",
        provenance_id=event.id,
        reason="Reviewed normalized processed provider settlement event",
    )

    preview = billing_service.consolidated_payment_settlements.preview_reconciliation(
        db_session, str(payment.id), request
    )

    assert preview.provenance_type == "provider_event"
    assert preview.provenance_id == event.id


def test_unallocated_remainder_requires_exact_billing_account_entry(db_session):
    _account, payment, allocation, subscriber_entry, _billing_entry, topup = (
        _historical_payment(db_session)
    )
    request = BillingAccountPaymentSettlementReconciliationRequest(
        allocation_ledger_entry_ids={allocation.id: subscriber_entry.id},
        provenance_type="topup_intent",
        provenance_id=topup.id,
        reason="Reviewed historical settlement with a missing remainder entry",
    )

    with pytest.raises(HTTPException, match="requires billing-account ledger evidence"):
        billing_service.consolidated_payment_settlements.preview_reconciliation(
            db_session, str(payment.id), request
        )

    assert db_session.query(PaymentSettlement).count() == 0
