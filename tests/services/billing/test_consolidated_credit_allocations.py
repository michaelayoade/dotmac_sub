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
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    PaymentAllocation,
    PaymentSettlementOrigin,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountCreditAllocationConfirm,
    BillingAccountCreditAllocationPreviewRequest,
    BillingAccountPaymentPreviewRequest,
    BillingAccountPaymentRefundRequest,
    PaymentRefundPreviewRequest,
)
from app.services import billing as billing_service


def _account_and_subscriber(db_session, *, receivable: str = "100.00"):
    reseller = Reseller(name=f"Credit owner {uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Credit",
        last_name="Member",
        email=f"credit-member-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal(receivable),
        balance_due=Decimal(receivable),
    )
    db_session.add(invoice)
    db_session.commit()
    return reseller, account, subscriber, invoice


def _fund(db_session, account, amount: str):
    request = BillingAccountPaymentPreviewRequest(
        amount=Decimal(amount), currency="NGN", auto_allocate=False
    )
    return billing_service.consolidated_payment_settlements.settle_verified(
        db_session,
        str(account.id),
        request,
        idempotency_key=f"credit-source-{uuid.uuid4()}",
        origin=PaymentSettlementOrigin.system,
    ).payment


def _command(preview, *, key: str):
    return BillingAccountCreditAllocationConfirm(
        amount=preview.allocation_amount,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key,
    )


def test_preview_is_read_only_and_separates_financial_positions(db_session):
    _reseller, account, subscriber, invoice = _account_and_subscriber(
        db_session, receivable="150.00"
    )
    _fund(db_session, account, "100.00")

    preview = billing_service.consolidated_credit_allocations.preview(
        db_session,
        str(account.id),
        str(subscriber.id),
        BillingAccountCreditAllocationPreviewRequest(amount=Decimal("75.00")),
    )

    assert preview.recorded_consolidated_credit == Decimal("100.00")
    assert preview.evidenced_consolidated_credit == Decimal("100.00")
    assert preview.unbacked_consolidated_credit == Decimal("0.00")
    assert preview.subscriber_receivable_before == Decimal("150.00")
    assert preview.subscriber_receivable_after == Decimal("75.00")
    assert preview.allocation_amount == Decimal("75.00")
    assert "no_direct_access_decision" in preview.service_access_consequence
    assert db_session.query(BillingAccountCreditAllocation).count() == 0
    db_session.refresh(account)
    db_session.refresh(invoice)
    assert account.balance == Decimal("100.00")
    assert invoice.balance_due == Decimal("150.00")


def test_partial_return_leaves_only_exact_remaining_source_credit(db_session):
    _reseller, account, subscriber, _invoice = _account_and_subscriber(db_session)
    payment = _fund(db_session, account, "100.00")
    refund_request = PaymentRefundPreviewRequest(
        amount=Decimal("30.00"), reason="Confirmed partial return"
    )
    refund_preview = billing_service.consolidated_payment_refunds.preview(
        db_session, str(payment.id), refund_request
    )
    billing_service.consolidated_payment_refunds.confirm(
        db_session,
        str(payment.id),
        BillingAccountPaymentRefundRequest(
            **refund_request.model_dump(),
            preview_fingerprint=refund_preview.fingerprint,
            idempotency_key="credit-source-partial-return",
        ),
    )

    preview = billing_service.consolidated_credit_allocations.preview(
        db_session,
        str(account.id),
        str(subscriber.id),
        BillingAccountCreditAllocationPreviewRequest(),
    )

    assert preview.recorded_consolidated_credit == Decimal("70.00")
    assert preview.evidenced_consolidated_credit == Decimal("70.00")
    assert preview.allocation_amount == Decimal("70.00")
    assert len(preview.source_effects) == 1
    assert preview.source_effects[0].payment_id == payment.id
    assert preview.source_effects[0].available_before == Decimal("70.00")
    assert preview.source_effects[0].consumed_amount == Decimal("70.00")


def test_confirm_links_multiple_sources_replays_and_audits(db_session):
    _reseller, account, subscriber, invoice = _account_and_subscriber(db_session)
    first_payment = _fund(db_session, account, "40.00")
    second_payment = _fund(db_session, account, "60.00")
    preview = billing_service.consolidated_credit_allocations.preview(
        db_session,
        str(account.id),
        str(subscriber.id),
        BillingAccountCreditAllocationPreviewRequest(),
    )
    command = _command(preview, key="credit-allocation-multi-source-replay")

    first = billing_service.consolidated_credit_allocations.confirm(
        db_session,
        str(account.id),
        str(subscriber.id),
        command,
        actor_id="reseller-operator-1",
    )
    replay = billing_service.consolidated_credit_allocations.confirm(
        db_session,
        str(account.id),
        str(subscriber.id),
        command,
        actor_id="reseller-operator-1",
    )

    items = (
        db_session.query(BillingAccountCreditAllocationItem)
        .filter(BillingAccountCreditAllocationItem.allocation_id == first.allocation_id)
        .all()
    )
    source_entries = (
        db_session.query(BillingAccountLedgerEntry)
        .filter(
            BillingAccountLedgerEntry.payment_id.in_(
                [first_payment.id, second_payment.id]
            )
        )
        .all()
    )
    result_entry = db_session.get(
        BillingAccountLedgerEntry, first.billing_account_ledger_entry_id
    )
    assert len(preview.source_effects) == 2
    assert len(source_entries) == 2
    assert len(items) == 2
    assert result_entry is not None
    assert result_entry.amount == Decimal("100.00")
    assert result_entry.balance_after == Decimal("0.00")
    assert {item.source_billing_account_ledger_entry_id for item in items} == {
        entry.id for entry in source_entries
    }
    assert set(first.payment_allocation_ids) == {
        item.payment_allocation_id for item in items
    }
    assert set(first.subscriber_ledger_entry_ids) == {
        item.subscriber_ledger_entry_id for item in items
    }
    assert all(
        db_session.get(PaymentAllocation, item.payment_allocation_id) is not None
        for item in items
    )
    assert all(
        db_session.get(LedgerEntry, item.subscriber_ledger_entry_id) is not None
        for item in items
    )
    assert replay.allocation_id == first.allocation_id
    assert replay.idempotent_replay is True
    assert db_session.query(BillingAccountCreditAllocation).count() == 1
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "allocate_consolidated_credit")
        .filter(AuditEvent.entity_id == str(first.allocation_id))
        .one()
    )
    assert audit.actor_id == "reseller-operator-1"
    assert audit.metadata_["billing_account_ledger_entry_id"] == str(result_entry.id)
    assert set(audit.metadata_["subscriber_ledger_entry_ids"]) == {
        str(item.subscriber_ledger_entry_id) for item in items
    }
    db_session.refresh(account)
    db_session.refresh(invoice)
    assert account.balance == Decimal("0.00")
    assert invoice.balance_due == Decimal("0.00")


def test_confirm_rejects_stale_preview_without_posting_money(db_session):
    _reseller, account, subscriber, invoice = _account_and_subscriber(db_session)
    _fund(db_session, account, "100.00")
    preview = billing_service.consolidated_credit_allocations.preview(
        db_session,
        str(account.id),
        str(subscriber.id),
        BillingAccountCreditAllocationPreviewRequest(amount=Decimal("80.00")),
    )
    invoice.balance_due = Decimal("50.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="Financial state changed"):
        billing_service.consolidated_credit_allocations.confirm(
            db_session,
            str(account.id),
            str(subscriber.id),
            _command(preview, key="credit-allocation-stale-preview"),
        )

    assert db_session.query(BillingAccountCreditAllocation).count() == 0
    assert db_session.query(BillingAccountCreditAllocationItem).count() == 0
    db_session.refresh(account)
    assert account.balance == Decimal("100.00")


def test_preview_rejects_subscriber_owned_by_another_reseller(db_session):
    _reseller, account, _subscriber, _invoice = _account_and_subscriber(db_session)
    _fund(db_session, account, "100.00")
    other = Reseller(name="Other reseller")
    db_session.add(other)
    db_session.flush()
    outsider = Subscriber(
        first_name="Outside",
        last_name="Scope",
        email=f"outside-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=other.id,
    )
    db_session.add(outsider)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        billing_service.consolidated_credit_allocations.preview(
            db_session,
            str(account.id),
            str(outsider.id),
            BillingAccountCreditAllocationPreviewRequest(),
        )

    assert exc.value.status_code == 404
    assert db_session.query(BillingAccountCreditAllocation).count() == 0


def test_preview_rejects_historical_debit_without_source_consumption(db_session):
    _reseller, account, subscriber, _invoice = _account_and_subscriber(db_session)
    _fund(db_session, account, "100.00")
    account.balance = Decimal("50.00")
    db_session.add(
        BillingAccountLedgerEntry(
            billing_account_id=account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.other,
            amount=Decimal("50.00"),
            currency="NGN",
            balance_after=Decimal("50.00"),
            memo="Historical debit without exact allocation evidence",
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException, match="historical debit"):
        billing_service.consolidated_credit_allocations.preview(
            db_session,
            str(account.id),
            str(subscriber.id),
            BillingAccountCreditAllocationPreviewRequest(),
        )

    assert db_session.query(BillingAccountCreditAllocation).count() == 0
