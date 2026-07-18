from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderType,
    TopupIntent,
)
from app.models.subscriber import SubscriberStatus
from app.schemas.billing import InvoiceCreate, PaymentSyncRead
from app.services.account_credit_deposits import (
    AccountCreditDeposits,
    DepositEligibilityError,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.account_credit import AccountCreditApplications
from app.services.billing.invoices import Invoices
from app.services.payment_gateway_adapter import PaymentGatewayTransaction


def _provider(db_session) -> PaymentProvider:
    provider = PaymentProvider(
        name="Deposit Paystack",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)
    return provider


def _intent(db_session, subscriber, provider, *, amount="10000.00") -> TopupIntent:
    intent, preview, replayed = AccountCreditDeposits.create_intent(
        db_session,
        account_id=subscriber.id,
        amount=amount,
        currency="NGN",
        minimum="1000.00",
        maximum="500000.00",
        reference=f"DEP-{subscriber.id.hex[:12]}-{amount}",
        provider_type="paystack",
        provider_id=provider.id,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key=f"account-credit-test-{subscriber.id}-{amount}",
        channel="test",
        created_by="pytest",
        metadata={},
    )
    assert not replayed
    assert preview.eligible_invoice_count == 0
    return intent


def _transaction(
    intent,
    *,
    amount=None,
    currency=None,
    external_id="gateway-deposit-1",
    metadata=None,
):
    return PaymentGatewayTransaction(
        provider_type="paystack",
        external_id=external_id,
        amount=Decimal(amount or str(intent.requested_amount)),
        currency=currency or intent.currency,
        metadata=(
            {"topup_intent_id": str(intent.id)} if metadata is None else metadata
        ),
        memo_prefix="Paystack",
    )


def test_intent_persists_typed_server_owned_contract(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    assert intent.purpose == "account_credit_deposit"
    assert intent.allocation_policy == "credit_only"
    assert intent.credit_application_policy == "pay_eligible_invoices"
    assert intent.policy_version == 1
    assert intent.preview_fingerprint and len(intent.preview_fingerprint) == 64
    assert intent.provider_id == provider.id
    assert intent.channel == "test"


def test_deposit_is_rejected_while_payable_invoice_exists(db_session, subscriber):
    provider = _provider(db_session)
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
        )
    )
    db_session.commit()

    with pytest.raises(DepositEligibilityError) as exc_info:
        _intent(db_session, subscriber, provider)

    assert exc_info.value.code == "deposit_payable_invoices_exist"


def test_confirmed_deposit_is_credit_only_and_grants_no_service(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    result = AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent),
    )

    assert result.application.applied == Decimal("0.00")
    assert result.payment.settlement is not None
    assert result.payment.settlement.unallocated_amount == Decimal("10000.00")
    assert result.payment.settlement.prepaid_amount == Decimal("0.00")
    assert result.payment.settlement.prepaid_ledger_entry_id is None
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "10000.00"
    )


def test_invoice_created_during_checkout_consumes_confirmed_credit(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="100000.00")
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-RACE",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("60000.00"),
        balance_due=Decimal("60000.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    result = AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-deposit-race"),
    )

    db_session.refresh(invoice)
    assert result.application.applied == Decimal("60000.00")
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "40000.00"
    )


def test_two_invoices_consume_one_credit_source_in_oldest_debt_order(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="9000.00")
    older = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-OLDER",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("6000.00"),
        balance_due=Decimal("6000.00"),
        due_at=datetime.now(UTC),
    )
    newer = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-NEWER",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("6000.00"),
        balance_due=Decimal("6000.00"),
        due_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([older, newer])
    db_session.commit()

    AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-two-invoices"),
    )

    db_session.refresh(older)
    db_session.refresh(newer)
    assert older.status == InvoiceStatus.paid
    assert newer.status == InvoiceStatus.partially_paid
    assert newer.balance_due == Decimal("3000.00")


def test_invoice_issued_after_deposit_uses_same_applicator(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="40000.00")
    settlement = AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-before-invoice"),
    )

    invoice = Invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            invoice_number="INV-AFTER-DEPOSIT",
            currency="NGN",
            subtotal=Decimal("60000.00"),
            total=Decimal("60000.00"),
            balance_due=Decimal("60000.00"),
            status=InvoiceStatus.issued,
        ),
    )

    assert invoice.status == InvoiceStatus.partially_paid
    assert invoice.balance_due == Decimal("20000.00")
    allocation = (
        db_session.query(PaymentAllocation)
        .filter_by(payment_id=settlement.payment.id, invoice_id=invoice.id)
        .one()
    )
    assert allocation.amount == Decimal("40000.00")
    assert allocation.ledger_entry_id is not None
    assert allocation.consumption_ledger_entry_id is not None


def test_voiding_invoice_releases_applied_account_credit(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    settlement = AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-before-void"),
    )
    invoice = Invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            invoice_number="INV-VOID-CREDIT",
            currency="NGN",
            subtotal=Decimal("6000.00"),
            total=Decimal("6000.00"),
            balance_due=Decimal("6000.00"),
            status=InvoiceStatus.issued,
        ),
    )
    allocation = (
        db_session.query(PaymentAllocation)
        .filter_by(payment_id=settlement.payment.id, invoice_id=invoice.id)
        .one()
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "4000.00"
    )

    result = Invoices.void_system(
        db_session,
        str(invoice.id),
        reason="Invoice canceled after account-credit application",
        idempotency_key="void-account-credit-allocation-0001",
    )

    db_session.refresh(allocation)
    assert result.invoice.status == InvoiceStatus.void
    assert allocation.is_active is False
    assert len(result.closure.ledger_evidence) == 2
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "10000.00"
    )


def test_draft_invoice_does_not_consume_credit_until_issued(db_session, subscriber):
    provider = _provider(db_session)
    draft = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DRAFT",
        status=InvoiceStatus.draft,
        currency="NGN",
        total=Decimal("5000.00"),
        balance_due=Decimal("5000.00"),
    )
    db_session.add(draft)
    db_session.commit()
    intent = _intent(db_session, subscriber, provider, amount="5000.00")
    AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-draft"),
    )
    assert (
        not db_session.query(PaymentAllocation).filter_by(invoice_id=draft.id).count()
    )

    Invoices.issue_draft_system(
        db_session,
        str(draft.id),
        issued_at=datetime.now(UTC),
        due_at=datetime.now(UTC),
        reason="test",
        commit=True,
    )
    db_session.refresh(draft)
    assert draft.status == InvoiceStatus.paid


def test_duplicate_confirmation_returns_same_payment(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    transaction = _transaction(intent, external_id="gateway-idempotent")

    first = AccountCreditDeposits.settle_verified(
        db_session, intent_id=intent.id, transaction=transaction
    )
    second = AccountCreditDeposits.settle_verified(
        db_session, intent_id=intent.id, transaction=transaction
    )

    assert second.already_recorded
    assert second.payment.id == first.payment.id
    assert (
        db_session.query(Payment).filter_by(external_id="gateway-idempotent").count()
        == 1
    )


def test_provider_amount_mismatch_posts_no_money(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    with pytest.raises(DepositEligibilityError) as exc_info:
        AccountCreditDeposits.settle_verified(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, amount="9999.00"),
        )

    assert exc_info.value.code == "deposit_amount_mismatch"
    assert db_session.query(Payment).count() == 0


def test_provider_currency_mismatch_posts_no_money(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    with pytest.raises(DepositEligibilityError) as exc_info:
        AccountCreditDeposits.settle_verified(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, currency="USD"),
        )

    assert exc_info.value.code == "deposit_currency_mismatch"
    assert db_session.query(Payment).count() == 0


def test_provider_correlation_mismatch_posts_no_money(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    with pytest.raises(DepositEligibilityError) as exc_info:
        AccountCreditDeposits.settle_verified(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, metadata={}),
        )

    assert exc_info.value.code == "deposit_provider_correlation_mismatch"
    assert db_session.query(Payment).count() == 0


def test_disabled_account_cannot_create_deposit(db_session, subscriber):
    provider = _provider(db_session)
    subscriber.status = SubscriberStatus.disabled
    db_session.commit()

    with pytest.raises(DepositEligibilityError) as exc_info:
        _intent(db_session, subscriber, provider)

    assert exc_info.value.code == "deposit_account_inactive"


def test_erp_payment_projection_carries_deposit_policy_and_settlement(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    result = AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-erp"),
    )

    projected = PaymentSyncRead.model_validate(result.payment)
    assert projected.intent_purpose == "account_credit_deposit"
    assert projected.allocation_policy == "credit_only"
    assert projected.credit_application_policy == "pay_eligible_invoices"
    assert projected.policy_version == 1
    assert projected.settlement is not None
    assert projected.settlement.unallocated_amount == Decimal("10000.00")


def test_invariant_monitor_ignores_incompatible_currency_credit(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-monitor"),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="USD",
        total=Decimal("1.00"),
        balance_due=Decimal("1.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    # Same-currency credit is required, so this mismatch is not an anomaly.
    assert not AccountCreditApplications.inspect_invariants(
        db_session, str(subscriber.id)
    )


def test_invariant_monitor_reports_payable_invoice_with_unused_credit(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    AccountCreditDeposits.settle_verified(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-monitor-positive"),
    )
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            currency="NGN",
            total=Decimal("1.00"),
            balance_due=Decimal("1.00"),
        )
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(
        db_session, str(subscriber.id)
    )

    assert [item.code for item in violations] == ["eligible_invoice_with_unused_credit"]


def test_invariant_monitor_reports_paid_invoice_without_settlement_evidence(
    db_session, subscriber
):
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.paid,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("0.00"),
        )
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(
        db_session, str(subscriber.id)
    )

    assert [item.code for item in violations] == ["paid_invoice_underfunded"]
