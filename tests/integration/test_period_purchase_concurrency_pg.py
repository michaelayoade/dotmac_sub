"""Independent PostgreSQL transactions for competing period checkouts/receipts."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Invoice,
    Payment,
    PaymentProvider,
    PaymentProviderType,
    TopupIntent,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services import prepaid_period_purchases as purchases
from app.services.owner_commands import CommandContext
from app.services.service_period_policy import PrepaidPeriodPurchasePolicy
from tests.prepaid_funding_helpers import (
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)


@pytest.fixture
def purchase_engine(cloned_database) -> Iterator[Engine]:
    engine = create_engine(cloned_database("heads"))
    try:
        yield engine
    finally:
        engine.dispose()


def _context():
    return CommandContext.system(
        actor="pytest:concurrency",
        scope="prepaid-period-purchase:settle",
        reason="Concurrent billing verification",
    )


def _setup(engine, monkeypatch):
    assert engine.dialect.name == "postgresql"
    monkeypatch.setattr(
        purchases, "_policy", lambda db: PrepaidPeriodPurchasePolicy(True, 12)
    )
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex
    now = datetime.now(UTC)
    with sessions() as db:
        reseller = Reseller(
            name=f"Purchase {suffix}", code=f"purchase-{suffix}", is_active=True
        )
        account = Subscriber(
            first_name="Purchase",
            last_name="Concurrency",
            email=f"{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Purchase {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
        )
        db.add_all([reseller, account, offer])
        db.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            unit_price=Decimal("100.00"),
            start_at=now - timedelta(days=31),
            next_billing_at=now - timedelta(days=1),
        )
        db.add(subscription)
        db.flush()
        ensure_test_prepaid_contract(db, subscription, "100.00")
        db.commit()
        materialize_test_prepaid_opening_balance(db, account.id, "0.00")
        quote = purchases.preview_prepaid_period_purchase(
            db,
            account_id=account.id,
            subscription_id=subscription.id,
            period_count=2,
            effective_at=now,
        )
        command = purchases.CreatePrepaidPeriodPurchaseCommand(
            account_id=account.id,
            subscription_id=subscription.id,
            period_count=2,
            expected_fingerprint=quote.fingerprint,
            idempotency_key=str(uuid4()),
            created_by="pytest",
            effective_at=now,
        )
        db.commit()
    return sessions, command


def test_concurrent_different_keys_do_not_sell_duplicate_coverage(
    purchase_engine, monkeypatch
):
    sessions, command = _setup(purchase_engine, monkeypatch)
    barrier = Barrier(2)

    def checkout(key):
        with sessions() as db:
            barrier.wait(timeout=10)
            try:
                return purchases.create_prepaid_period_purchase(
                    db, replace(command, idempotency_key=key), context=_context()
                ).id
            except purchases.PrepaidPeriodPurchaseError as exc:
                assert (
                    exc.code
                    == "financial.prepaid_period_purchases.checkout_in_progress"
                )
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(checkout, (str(uuid4()), str(uuid4()))))
    assert sum(value is not None for value in outcomes) == 1
    with sessions() as db:
        assert (
            db.scalar(
                select(func.count(PrepaidPeriodPurchase.id)).where(
                    PrepaidPeriodPurchase.subscription_id == command.subscription_id
                )
            )
            == 1
        )


def test_concurrent_webhook_and_verification_converge_on_one_settlement(
    purchase_engine, monkeypatch
):
    sessions, quote_command = _setup(purchase_engine, monkeypatch)
    with sessions() as db:
        purchase = purchases.create_prepaid_period_purchase(
            db, quote_command, context=_context()
        )
        provider = PaymentProvider(
            name=str(uuid4()),
            provider_type=PaymentProviderType.paystack,
            is_active=True,
        )
        db.add(provider)
        db.flush()
        intent = TopupIntent(
            account_id=purchase.account_id,
            provider_id=provider.id,
            provider_type="paystack",
            reference=str(uuid4()),
            purpose="prepaid_period_purchase",
            allocation_policy="selected_purchase_invoices_only",
            credit_application_policy="none",
            policy_version=1,
            preview_fingerprint=purchase.preview_fingerprint,
            idempotency_key=purchase.idempotency_key,
            channel="customer_selfcare",
            currency="NGN",
            requested_amount=purchase.total,
            expires_at=purchase.expires_at,
            status="pending",
        )
        db.add(intent)
        db.flush()
        purchase.topup_intent_id = intent.id
        purchase.status = PrepaidPeriodPurchaseStatus.payment_pending
        command = purchases.SettleVerifiedPrepaidPeriodPurchaseCommand(
            intent_id=intent.id,
            provider_id=provider.id,
            external_transaction_id=str(uuid4()),
            amount=purchase.total,
            provider_fee=Decimal("0.00"),
            currency="NGN",
            effective_at=datetime.now(UTC),
        )
        db.commit()
    barrier = Barrier(2)

    def settle(_):
        with sessions() as db:
            barrier.wait(timeout=10)
            return purchases.settle_verified_prepaid_period_purchase(
                db, command, context=_context()
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(settle, range(2)))
    assert all(
        result.status is PrepaidPeriodPurchaseStatus.completed for result in results
    )
    assert results[0].invoice_ids == results[1].invoice_ids
    assert results[0].payment_id == results[1].payment_id
    with sessions() as db:
        assert (
            db.scalar(
                select(func.count(Payment.id)).where(
                    Payment.account_id == quote_command.account_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Invoice.id)).where(
                    Invoice.account_id == quote_command.account_id
                )
            )
            == 2
        )
