from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    BillingAccountCreditAllocation,
    BillingAccountCreditAllocationItem,
    BillingAccountLedgerEntry,
    ConsolidatedCreditConsumptionReconciliationEvidence,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountCreditAllocationPreviewRequest,
    BillingAccountCreditConsumptionReconciliationConfirm,
    BillingAccountCreditConsumptionReconciliationRequest,
    BillingAccountCreditConsumptionSelection,
    BillingAccountPaymentPreviewRequest,
)
from app.services import billing as billing_service


def _historical_consumption(
    db_session,
    *,
    amount: str = "40.00",
    recorded_balance: str | None = None,
    existing_debit: bool = False,
):
    reseller = Reseller(name=f"Historical credit {uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Historical",
        last_name="Credit",
        email=f"historical-credit-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("100.00"),
        balance_due=Decimal("100.00") - Decimal(amount),
    )
    db_session.add(invoice)
    db_session.flush()
    payment = billing_service.consolidated_payment_settlements.settle_verified(
        db_session,
        str(account.id),
        BillingAccountPaymentPreviewRequest(
            amount=Decimal("100.00"), currency="NGN", auto_allocate=False
        ),
        idempotency_key=f"historical-credit-source-{uuid.uuid4()}",
        origin=PaymentSettlementOrigin.system,
    ).payment
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal(amount),
        memo="Historical consolidated-credit allocation",
        is_active=True,
    )
    subscriber_entry = LedgerEntry(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal(amount),
        currency="NGN",
        memo="Historical allocation result",
    )
    db_session.add_all([allocation, subscriber_entry])
    db_session.flush()
    debit = None
    expected_balance = Decimal("100.00") - Decimal(amount)
    account.balance = (
        Decimal(recorded_balance) if recorded_balance is not None else expected_balance
    )
    if existing_debit:
        debit = BillingAccountLedgerEntry(
            billing_account_id=account.id,
            payment_id=None,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.other,
            amount=Decimal(amount),
            currency="NGN",
            balance_after=expected_balance,
            memo="Reviewed historical debit",
        )
        db_session.add(debit)
    db_session.commit()
    source = db_session.get(
        BillingAccountLedgerEntry,
        payment.settlement.billing_account_ledger_entry_id,
    )
    assert source is not None
    return (
        account,
        subscriber,
        invoice,
        payment,
        allocation,
        subscriber_entry,
        source,
        debit,
    )


def _request(allocation, subscriber_entry, source, debit=None):
    return BillingAccountCreditConsumptionReconciliationRequest(
        allocation_evidence=[
            BillingAccountCreditConsumptionSelection(
                payment_allocation_id=allocation.id,
                subscriber_ledger_entry_id=subscriber_entry.id,
                source_billing_account_ledger_entry_id=source.id,
            )
        ],
        billing_account_debit_ledger_entry_id=debit.id if debit else None,
        create_missing_billing_account_debit=debit is None,
        reason="Reviewed exact historical consolidated-credit consumption",
    )


def _command(request, preview, key: str):
    return BillingAccountCreditConsumptionReconciliationConfirm(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key,
    )


def test_inspection_is_read_only_and_separates_projection_drift(db_session):
    account, _subscriber, _invoice, _payment, allocation, entry, source, _debit = (
        _historical_consumption(db_session)
    )

    inspection = (
        billing_service.consolidated_credit_allocations.inspect_reconciliation_evidence(
            db_session, str(account.id)
        )
    )

    assert inspection.recorded_consolidated_credit == Decimal("60.00")
    assert inspection.evidenced_consolidated_credit == Decimal("100.00")
    assert inspection.projection_drift == Decimal("-40.00")
    assert inspection.unbacked_projection_amount == Decimal("0.00")
    assert inspection.missing_debit_projection_amount == Decimal("40.00")
    assert inspection.source_candidates[0].billing_account_ledger_entry_id == source.id
    assert inspection.source_candidates[0].available_amount == Decimal("100.00")
    candidate = inspection.allocation_candidates[0]
    assert candidate.payment_allocation_id == allocation.id
    assert candidate.payment_has_settlement is True
    assert candidate.subscriber_ledger_entry_ids == [entry.id]
    assert inspection.debit_candidates == []
    assert "no_access_decision" in inspection.service_access_consequence
    assert db_session.query(BillingAccountCreditAllocation).count() == 0
    assert db_session.query(BillingAccountCreditAllocationItem).count() == 0
    db_session.refresh(account)
    assert account.balance == Decimal("60.00")


def test_preview_confirm_missing_debit_replays_and_unblocks_owner(db_session):
    account, subscriber, _invoice, _payment, allocation, entry, source, _debit = (
        _historical_consumption(db_session)
    )
    request = _request(allocation, entry, source)
    preview = billing_service.consolidated_credit_allocations.preview_reconciliation(
        db_session, str(account.id), request
    )
    billing_entries_before = db_session.query(BillingAccountLedgerEntry).count()

    assert preview.billing_account_debit_action == "created_missing"
    assert preview.ledger_transaction_created is True
    assert preview.billing_account_balance_changed is False
    assert preview.recorded_consolidated_credit_before == Decimal("60.00")
    assert preview.recorded_consolidated_credit_after == Decimal("60.00")
    assert preview.evidenced_consolidated_credit_before == Decimal("100.00")
    assert preview.evidenced_consolidated_credit_after == Decimal("60.00")
    assert preview.projection_drift_before == Decimal("-40.00")
    assert preview.projection_drift_after == Decimal("0.00")

    command = _command(request, preview, "historical-credit-missing-debit-replay")
    result = billing_service.consolidated_credit_allocations.reconcile_historical_consumption(
        db_session,
        str(account.id),
        command,
        actor_id="billing-reconciliation-operator",
    )
    replay = billing_service.consolidated_credit_allocations.reconcile_historical_consumption(
        db_session,
        str(account.id),
        command,
        actor_id="billing-reconciliation-operator",
    )

    assert result.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.allocation_id == result.allocation_id
    assert result.billing_account_debit_action == "created_missing"
    assert result.billing_account_balance_changed is False
    assert (
        db_session.query(BillingAccountLedgerEntry).count()
        == billing_entries_before + 1
    )
    db_session.refresh(account)
    db_session.refresh(allocation)
    assert account.balance == Decimal("60.00")
    assert allocation.ledger_entry_id == entry.id
    decision = db_session.get(BillingAccountCreditAllocation, result.allocation_id)
    assert decision is not None
    assert decision.billing_account_ledger_entry.amount == Decimal("40.00")
    assert decision.billing_account_ledger_entry.balance_after == Decimal("60.00")
    assert len(decision.items) == 1
    assert decision.items[0].source_billing_account_ledger_entry_id == source.id
    evidence = db_session.get(
        ConsolidatedCreditConsumptionReconciliationEvidence,
        result.reconciliation_evidence_id,
    )
    assert evidence is not None
    assert evidence.debit_action == "created_missing"
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "reconcile_consolidated_credit_consumption")
        .one()
    )
    assert audit.actor_id == "billing-reconciliation-operator"
    assert audit.metadata_["billing_account_balance_changed"] is False
    assert audit.metadata_["ledger_transaction_created"] is True

    owner_preview = billing_service.consolidated_credit_allocations.preview(
        db_session,
        str(account.id),
        str(subscriber.id),
        BillingAccountCreditAllocationPreviewRequest(),
    )
    assert owner_preview.evidenced_consolidated_credit == Decimal("60.00")
    assert owner_preview.allocation_amount == Decimal("60.00")


def test_existing_debit_is_linked_without_posting_another_transaction(db_session):
    account, _subscriber, _invoice, _payment, allocation, entry, source, debit = (
        _historical_consumption(db_session, amount="30.00", existing_debit=True)
    )
    assert debit is not None
    request = _request(allocation, entry, source, debit)
    preview = billing_service.consolidated_credit_allocations.preview_reconciliation(
        db_session, str(account.id), request
    )
    billing_entries_before = db_session.query(BillingAccountLedgerEntry).count()

    assert preview.billing_account_debit_action == "linked_existing"
    assert preview.billing_account_debit_ledger_entry_id == debit.id
    assert preview.ledger_transaction_created is False
    assert preview.evidenced_consolidated_credit_before == Decimal("70.00")
    assert preview.evidenced_consolidated_credit_after == Decimal("70.00")

    result = billing_service.consolidated_credit_allocations.reconcile_historical_consumption(
        db_session,
        str(account.id),
        _command(request, preview, "historical-credit-existing-debit"),
    )

    assert result.billing_account_ledger_entry_id == debit.id
    assert result.billing_account_debit_action == "linked_existing"
    assert db_session.query(BillingAccountLedgerEntry).count() == billing_entries_before
    db_session.refresh(account)
    assert account.balance == Decimal("70.00")


def test_unbacked_positive_projection_cannot_create_consumption_evidence(db_session):
    account, _subscriber, _invoice, _payment, allocation, entry, source, _debit = (
        _historical_consumption(db_session, amount="20.00", recorded_balance="120.00")
    )
    inspection = (
        billing_service.consolidated_credit_allocations.inspect_reconciliation_evidence(
            db_session, str(account.id)
        )
    )
    assert inspection.unbacked_projection_amount == Decimal("20.00")
    assert inspection.missing_debit_projection_amount == Decimal("0.00")

    with pytest.raises(HTTPException, match="missing-debit projection drift"):
        billing_service.consolidated_credit_allocations.preview_reconciliation(
            db_session, str(account.id), _request(allocation, entry, source)
        )

    assert db_session.query(BillingAccountCreditAllocation).count() == 0


def test_confirmation_rejects_stale_projection_and_posts_nothing(db_session):
    account, _subscriber, _invoice, _payment, allocation, entry, source, _debit = (
        _historical_consumption(db_session)
    )
    request = _request(allocation, entry, source)
    preview = billing_service.consolidated_credit_allocations.preview_reconciliation(
        db_session, str(account.id), request
    )
    account.balance = Decimal("50.00")
    db_session.commit()
    billing_entries_before = db_session.query(BillingAccountLedgerEntry).count()

    with pytest.raises(HTTPException, match="Financial evidence changed"):
        billing_service.consolidated_credit_allocations.reconcile_historical_consumption(
            db_session,
            str(account.id),
            _command(request, preview, "historical-credit-stale-preview"),
        )

    assert db_session.query(BillingAccountLedgerEntry).count() == billing_entries_before
    assert db_session.query(BillingAccountCreditAllocation).count() == 0


def test_unsettled_historical_carrier_is_not_promoted_to_cash(db_session):
    account, _subscriber, invoice, _payment, _allocation, _entry, source, _debit = (
        _historical_consumption(db_session)
    )
    carrier = Payment(
        billing_account_id=account.id,
        amount=Decimal("10.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        memo="Historical carrier without cash provenance",
    )
    db_session.add(carrier)
    db_session.flush()
    allocation = PaymentAllocation(
        payment_id=carrier.id,
        invoice_id=invoice.id,
        amount=Decimal("10.00"),
        is_active=True,
    )
    entry = LedgerEntry(
        account_id=invoice.account_id,
        invoice_id=invoice.id,
        payment_id=carrier.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("10.00"),
        currency="NGN",
    )
    db_session.add_all([allocation, entry])
    db_session.commit()
    request = _request(allocation, entry, source)

    with pytest.raises(HTTPException, match="lacks exact consolidated settlement"):
        billing_service.consolidated_credit_allocations.preview_reconciliation(
            db_session, str(account.id), request
        )

    assert carrier.settlement is None
    assert db_session.query(BillingAccountCreditAllocation).count() == 0
