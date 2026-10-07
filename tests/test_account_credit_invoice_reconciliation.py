from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    TopupIntent,
)
from app.services.account_credit_invoice_reconciliation import (
    RECONCILIATION_SCOPE,
    AccountCreditInvoiceReconciliationDisposition,
    AccountCreditInvoiceReconciliationQuery,
    ReconcileAccountCreditInvoiceCommand,
    preview_account_credit_invoice_reconciliation,
    reconcile_account_credit_invoice,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _evidence(db_session, subscriber, *, subscription_id=None):
    now = datetime.now(UTC)
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("35634.52"),
        provider_fee=Decimal("634.52"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=now - timedelta(days=30),
        auto_allocate_on_settlement=False,
        is_active=True,
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-RECONCILE-TEST",
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal("35000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("35000.00"),
        balance_due=Decimal("35000.00"),
        issued_at=now - timedelta(days=29),
        due_at=now - timedelta(days=1),
        is_active=True,
        is_proforma=False,
    )
    db_session.add_all((payment, invoice))
    db_session.flush()
    credit = LedgerEntry(
        account_id=subscriber.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("35000.00"),
        currency="NGN",
        memo="Exact settled deposit credit",
    )
    db_session.add(credit)
    db_session.flush()
    settlement = PaymentSettlement(
        payment_id=payment.id,
        unallocated_ledger_entry_id=credit.id,
        amount=Decimal("35000.00"),
        unallocated_amount=Decimal("35000.00"),
        prepaid_amount=Decimal("0.00"),
        currency="NGN",
        origin=PaymentSettlementOrigin.provider_event,
        idempotency_key=f"pytest:settlement:{payment.id}",
    )
    intent = TopupIntent(
        account_id=subscriber.id,
        completed_payment_id=payment.id,
        purpose="account_credit_deposit",
        allocation_policy="credit_only",
        credit_application_policy="pay_eligible_invoices",
        policy_version=1,
        preview_fingerprint="a" * 64,
        idempotency_key=f"pytest:deposit:{payment.id}",
        channel="customer_selfcare",
        reference=f"DEP-{payment.id}",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("35000.00"),
        actual_amount=Decimal("35634.52"),
        status="completed",
        completed_at=now - timedelta(days=30),
    )
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription_id,
        description="Indoor Cable Replacement",
        quantity=Decimal("1.000"),
        unit_price=Decimal("35000.00"),
        amount=Decimal("35000.00"),
        is_active=True,
    )
    db_session.add_all((settlement, intent, line))
    db_session.commit()
    return invoice, payment, settlement, intent


def _query(subscriber, invoice, payment, intent):
    return AccountCreditInvoiceReconciliationQuery(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        topup_intent_id=intent.id,
        expected_amount=Decimal("35000.00"),
        currency="NGN",
    )


def test_reconciliation_uses_existing_payment_and_is_replay_safe(
    db_session, subscriber
):
    invoice, payment, settlement, intent = _evidence(db_session, subscriber)
    query = _query(subscriber, invoice, payment, intent)
    preview = preview_account_credit_invoice_reconciliation(db_session, query)

    assert preview.disposition is AccountCreditInvoiceReconciliationDisposition.eligible
    assert preview.payment_available == Decimal("35000.00")
    payment_count = db_session.query(Payment).count()
    db_session_adapter.release_read_transaction(db_session)
    context = CommandContext.system(
        actor="pytest:finance",
        scope=RECONCILIATION_SCOPE,
        reason="Repair stranded deposit credit",
        idempotency_key="pytest-account-credit-reconcile-0001",
    )
    command = ReconcileAccountCreditInvoiceCommand(
        query=query,
        expected_preview_fingerprint=preview.fingerprint,
        permission_granted=True,
        authorized_system_user_id=uuid4(),
    )

    result = reconcile_account_credit_invoice(db_session, command, context=context)
    replay = reconcile_account_credit_invoice(db_session, command, context=context)

    assert not result.replayed
    assert replay.replayed
    assert replay.allocation_id == result.allocation_id
    assert result.settlement_id == settlement.id
    assert db_session.query(Payment).count() == payment_count
    allocation = db_session.get(PaymentAllocation, result.allocation_id)
    db_session.refresh(invoice)
    assert allocation is not None
    assert allocation.payment_id == payment.id
    assert allocation.amount == Decimal("35000.00")
    assert allocation.ledger_entry_id == result.invoice_ledger_entry_id
    assert (
        allocation.consumption_ledger_entry_id
        == result.credit_consumption_ledger_entry_id
    )
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")


def test_service_invoice_is_not_actionable(db_session, subscriber, subscription):
    invoice, payment, _settlement, intent = _evidence(
        db_session, subscriber, subscription_id=subscription.id
    )

    preview = preview_account_credit_invoice_reconciliation(
        db_session, _query(subscriber, invoice, payment, intent)
    )

    assert (
        preview.disposition
        is AccountCreditInvoiceReconciliationDisposition.manual_review
    )
    assert "non-service" in preview.reason
