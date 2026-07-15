from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    BillingAccountLedgerEntry,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountPaymentConfirm,
    BillingAccountPaymentPreviewRequest,
    PaymentCreate,
)
from app.services import billing as billing_service


def _account_and_invoice(db_session, *, invoice_amount: str = "100.00"):
    reseller = Reseller(name=f"Owner-{uuid.uuid4().hex[:8]}")
    db_session.add(reseller)
    db_session.flush()
    billing_account = billing_service.billing_accounts.create_default_for_reseller(
        db_session, str(reseller.id)
    )
    subscriber = Subscriber(
        first_name="Consolidated",
        last_name="Member",
        email=f"member-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller.id,
    )
    db_session.add(subscriber)
    db_session.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal(invoice_amount),
        balance_due=Decimal(invoice_amount),
    )
    db_session.add(invoice)
    db_session.commit()
    return billing_account, subscriber, invoice


def _command(request, fingerprint: str, key: str):
    return BillingAccountPaymentConfirm(
        **request.model_dump(),
        preview_fingerprint=fingerprint,
        idempotency_key=key,
    )


def test_preview_is_read_only_and_keeps_financial_states_distinct(db_session):
    account, _subscriber, invoice = _account_and_invoice(db_session)
    request = BillingAccountPaymentPreviewRequest(
        amount=Decimal("125.00"), currency="NGN"
    )

    preview = billing_service.consolidated_payment_settlements.preview(
        db_session, str(account.id), request
    )

    assert preview.payment_state == PaymentStatus.succeeded
    assert preview.allocated_amount == Decimal("100.00")
    assert preview.unallocated_amount == Decimal("25.00")
    assert preview.consolidated_credit_before == Decimal("0.00")
    assert preview.consolidated_credit_after == Decimal("25.00")
    assert preview.allocation_effects[0].receivable_before == Decimal("100.00")
    assert preview.allocation_effects[0].receivable_after == Decimal("0.00")
    assert "no_direct_access_decision" in preview.service_access_consequence
    assert db_session.query(PaymentSettlement).count() == 0
    assert db_session.query(BillingAccountLedgerEntry).count() == 0
    db_session.refresh(invoice)
    db_session.refresh(account)
    assert invoice.balance_due == Decimal("100.00")
    assert account.balance == Decimal("0.00")


def test_confirmation_links_every_exact_ledger_result_and_audit(db_session):
    account, subscriber, invoice = _account_and_invoice(db_session)
    request = BillingAccountPaymentPreviewRequest(
        amount=Decimal("125.00"), currency="NGN", memo="verified transfer"
    )
    preview = billing_service.consolidated_payment_settlements.preview(
        db_session, str(account.id), request
    )

    result = billing_service.consolidated_payment_settlements.confirm(
        db_session,
        str(account.id),
        _command(request, preview.fingerprint, "test-consolidated-exact-ledger"),
        origin=PaymentSettlementOrigin.manual,
        actor_id="operator-1",
    )

    db_session.refresh(account)
    db_session.refresh(invoice)
    allocation = result.payment.allocations[0]
    subscriber_entry = db_session.get(LedgerEntry, allocation.ledger_entry_id)
    account_entry = db_session.get(
        BillingAccountLedgerEntry,
        result.settlement.billing_account_ledger_entry_id,
    )
    assert subscriber_entry is not None
    assert subscriber_entry.account_id == subscriber.id
    assert subscriber_entry.invoice_id == invoice.id
    assert subscriber_entry.amount == Decimal("100.00")
    assert account_entry is not None
    assert account_entry.payment_id == result.payment.id
    assert account_entry.amount == Decimal("25.00")
    assert account_entry.balance_after == Decimal("25.00")
    assert result.settlement.unallocated_amount == Decimal("25.00")
    assert result.settlement.prepaid_amount == Decimal("0.00")
    assert account.balance == Decimal("25.00")
    assert invoice.balance_due == Decimal("0.00")
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "settle_consolidated_payment")
        .filter(AuditEvent.entity_id == str(result.payment.id))
        .one()
    )
    assert audit.actor_id == "operator-1"
    assert audit.metadata_["billing_account_ledger_entry_id"] == str(account_entry.id)
    assert audit.metadata_["allocation_ledger_entry_ids"] == [str(subscriber_entry.id)]


def test_confirmation_rejects_stale_preview_without_money_effect(db_session):
    account, _subscriber, invoice = _account_and_invoice(db_session)
    request = BillingAccountPaymentPreviewRequest(
        amount=Decimal("100.00"), currency="NGN"
    )
    preview = billing_service.consolidated_payment_settlements.preview(
        db_session, str(account.id), request
    )
    invoice.balance_due = Decimal("75.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="Financial state changed"):
        billing_service.consolidated_payment_settlements.confirm(
            db_session,
            str(account.id),
            _command(request, preview.fingerprint, "test-consolidated-stale-preview"),
        )

    db_session.refresh(account)
    assert account.balance == Decimal("0.00")
    assert db_session.query(PaymentSettlement).count() == 0


def test_confirmation_replay_is_idempotent(db_session):
    account, _subscriber, _invoice = _account_and_invoice(db_session)
    request = BillingAccountPaymentPreviewRequest(
        amount=Decimal("100.00"), currency="NGN"
    )
    preview = billing_service.consolidated_payment_settlements.preview(
        db_session, str(account.id), request
    )
    command = _command(
        request, preview.fingerprint, "test-consolidated-idempotent-replay"
    )

    first = billing_service.consolidated_payment_settlements.confirm(
        db_session, str(account.id), command
    )
    replay = billing_service.consolidated_payment_settlements.confirm(
        db_session, str(account.id), command
    )

    assert replay.idempotent_replay is True
    assert replay.payment.id == first.payment.id
    assert replay.settlement.id == first.settlement.id
    assert db_session.query(PaymentSettlement).count() == 1


def test_generic_payment_writer_gates_confirmed_consolidated_money(db_session):
    account, _subscriber, _invoice = _account_and_invoice(db_session)

    with pytest.raises(HTTPException, match="dedicated preview"):
        billing_service.payments.create(
            db_session,
            PaymentCreate(
                billing_account_id=account.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
            ),
        )

    db_session.refresh(account)
    assert account.balance == Decimal("0.00")
    assert db_session.query(PaymentSettlement).count() == 0
