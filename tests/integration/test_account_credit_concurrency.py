"""PostgreSQL row-lock coverage for account-credit application."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderType,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.account_credit_deposits import AccountCreditDeposits
from app.services.billing._common import get_account_credit_balance
from app.services.billing.account_credit import AccountCreditApplications
from app.services.payment_gateway_adapter import PaymentGatewayTransaction


def test_two_applicators_cannot_spend_one_credit_source_twice(engine):
    """The account row lock serializes two independent PostgreSQL sessions."""
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Credit Concurrency {suffix}",
            code=f"credit-concurrency-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Credit",
            last_name="Concurrency",
            email=f"credit-concurrency-{suffix}@example.com",
            reseller=reseller,
        )
        provider = PaymentProvider(
            name=f"Credit Concurrency Paystack {suffix}",
            provider_type=PaymentProviderType.paystack,
            is_active=True,
        )
        setup.add_all([reseller, account, provider])
        setup.commit()
        intent, _preview, _replayed = AccountCreditDeposits.create_intent(
            setup,
            account_id=account.id,
            amount="10000.00",
            currency="NGN",
            minimum="1000.00",
            maximum="500000.00",
            reference=f"pg-credit-{suffix}",
            provider_type="paystack",
            provider_id=provider.id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=f"pg-credit-intent-{suffix}",
            channel="postgres_integration_test",
            created_by="pytest",
        )
        AccountCreditDeposits.settle_verified(
            setup,
            intent_id=intent.id,
            transaction=PaymentGatewayTransaction(
                provider_type="paystack",
                external_id=f"pg-credit-payment-{suffix}",
                amount=Decimal("10000.00"),
                currency="NGN",
                metadata={"topup_intent_id": str(intent.id)},
                memo_prefix="Postgres test",
            ),
        )
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"INV-PG-CREDIT-{suffix}",
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=Decimal("10000.00"),
            total=Decimal("10000.00"),
            balance_due=Decimal("10000.00"),
        )
        setup.add(invoice)
        setup.commit()
        account_id = str(account.id)
        invoice_id = invoice.id

    barrier = Barrier(2)

    def apply_credit() -> Decimal:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            result = AccountCreditApplications.apply(worker, account_id)
            worker.commit()
            return result.applied

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: apply_credit(), range(2)))

    with session_factory() as check:
        invoice = check.get(Invoice, invoice_id)
        allocations = (
            check.query(PaymentAllocation)
            .filter(PaymentAllocation.invoice_id == invoice_id)
            .filter(PaymentAllocation.is_active.is_(True))
            .all()
        )
        assert sum(results, Decimal("0.00")) == Decimal("10000.00")
        assert invoice is not None and invoice.status == InvoiceStatus.paid
        assert len(allocations) == 1
        assert allocations[0].amount == Decimal("10000.00")
        assert get_account_credit_balance(check, account_id) == Decimal("0.00")
