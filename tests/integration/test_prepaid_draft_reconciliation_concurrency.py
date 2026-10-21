"""PostgreSQL serialization coverage for mixed-source prepaid reconciliation."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
    PrepaidOpeningFundingConsumption,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    CreateReviewedPaidPrepaidInvoiceCommand,
    MissingPaidPrepaidInvoiceRepairQuery,
    ReconcilePrepaidDraftCommand,
    create_reviewed_paid_prepaid_invoice,
    preview_missing_paid_prepaid_invoice_repair,
    preview_prepaid_draft_reconciliation,
    reconcile_prepaid_draft_invoice,
)
from app.services.prepaid_service_renewals import (
    LegacyPaymentBackedRenewalInvoiceRepairQuery,
    PrepaidServiceRenewalSource,
    RepairLegacyPaymentBackedRenewalInvoiceCommand,
    preview_legacy_payment_backed_renewal_invoice_repair,
    repair_legacy_payment_backed_renewal_invoice,
    stage_prepaid_service_renewed_outcome,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def test_concurrent_opening_funding_confirmations_converge_on_one_consumption(
    engine,
) -> None:
    """The account and baseline locks make the second confirmation a replay."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    effective_at = datetime.now(UTC).replace(microsecond=0)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Opening Funding {suffix}",
            code=f"opening-funding-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Opening",
            last_name="Concurrency",
            email=f"opening-concurrency-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Opening Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            unit_price=Decimal("100.00"),
            start_at=effective_at - timedelta(days=31),
            next_billing_at=effective_at - timedelta(days=1),
        )
        setup.add(subscription)
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"INV-OPENING-{suffix}",
            status=InvoiceStatus.draft,
            currency="NGN",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            billing_period_start=effective_at - timedelta(days=1),
            billing_period_end=effective_at + timedelta(days=29),
            is_proforma=False,
            is_active=True,
        )
        setup.add(invoice)
        setup.flush()
        setup.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Prepaid service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("100.00"),
                amount=Decimal("100.00"),
                is_active=True,
            )
        )
        setup.commit()
        materialize_test_prepaid_opening_balance(
            setup,
            account.id,
            Decimal("100.00"),
            position_at=effective_at - timedelta(days=2),
        )
        preview = preview_prepaid_draft_reconciliation(setup, invoice.id)
        command = ReconcilePrepaidDraftCommand(
            context=CommandContext.system(
                actor="pytest:billing-operator",
                scope="prepaid_draft_reconciliation",
                reason="Concurrent reviewed opening-funding reconciliation",
                idempotency_key=f"pg-opening-reconciliation-{suffix}",
            ),
            invoice_id=invoice.id,
            preview_fingerprint=preview.fingerprint,
            effective_at=effective_at,
        )
        setup.commit()
        account_id = account.id
        invoice_id = invoice.id

    try:
        barrier = Barrier(2)

        def reconcile() -> tuple[uuid.UUID | None, bool]:
            with session_factory() as worker:
                barrier.wait(timeout=10)
                result = reconcile_prepaid_draft_invoice(worker, command)
                return result.opening_funding_consumption_id, result.replayed

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: reconcile(), range(2)))

        assert len({consumption_id for consumption_id, _replayed in results}) == 1
        assert sorted(replayed for _consumption_id, replayed in results) == [
            False,
            True,
        ]
        with session_factory() as check:
            invoice = check.get(Invoice, invoice_id)
            assert invoice is not None
            assert invoice.status is InvoiceStatus.paid
            assert invoice.balance_due == Decimal("0.00")
            assert (
                check.query(PrepaidOpeningFundingConsumption)
                .filter_by(invoice_id=invoice_id)
                .count()
                == 1
            )
    finally:
        # This test needs independent committed sessions to exercise PostgreSQL
        # row locks. Remove its one-time authority record so the session-scoped
        # integration database retains the same empty native-install state for
        # the tests that follow.
        with session_factory() as cleanup:
            baseline = (
                cleanup.query(PrepaidFundingBaseline)
                .filter_by(account_id=account_id)
                .one_or_none()
            )
            cleanup.query(PrepaidOpeningFundingConsumption).filter_by(
                invoice_id=invoice_id
            ).delete(synchronize_session=False)
            if baseline is not None:
                batch_id = baseline.batch_id
                cleanup.delete(baseline)
                cleanup.flush()
                cleanup.query(PrepaidFundingReconstructionBatch).filter_by(
                    id=batch_id
                ).delete(synchronize_session=False)
            cleanup.commit()


def test_concurrent_missing_invoice_repairs_converge_on_one_paid_invoice(
    engine,
) -> None:
    """Account locking and one idempotency key admit one reviewed invoice."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    paid_at = datetime(2026, 8, 6, 8, 22, 57, tzinfo=UTC)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Missing Invoice {suffix}",
            code=f"missing-invoice-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Missing",
            last_name="Invoice",
            email=f"missing-invoice-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Missing Invoice Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        setup.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            unit_price=Decimal("100.00"),
            start_at=datetime(2026, 7, 3, tzinfo=UTC),
            next_billing_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
        setup.add(subscription)
        setup.flush()
        payment = Payment(
            account_id=account.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=paid_at,
            is_active=True,
            created_at=paid_at,
        )
        setup.add(payment)
        setup.flush()
        credit = LedgerEntry(
            account_id=account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("100.00"),
            currency="NGN",
            memo="Concurrent reviewed payment",
            is_active=True,
            effective_date=paid_at,
            created_at=paid_at,
        )
        setup.add(credit)
        setup.flush()
        setup.add(
            PaymentSettlement(
                payment_id=payment.id,
                unallocated_ledger_entry_id=credit.id,
                amount=Decimal("100.00"),
                unallocated_amount=Decimal("100.00"),
                prepaid_amount=Decimal("0.00"),
                currency="NGN",
                origin=PaymentSettlementOrigin.system,
                idempotency_key=f"pg-missing-invoice-settlement-{suffix}",
                created_at=paid_at,
            )
        )
        setup.commit()
        query = MissingPaidPrepaidInvoiceRepairQuery(
            account_id=account.id,
            subscription_id=subscription.id,
            payment_id=payment.id,
            issued_on=date(2026, 8, 6),
            due_on=date(2026, 9, 5),
            next_billing_on=date(2026, 9, 5),
            expected_total=Decimal("100.00"),
            expected_remaining_credit=Decimal("0.00"),
        )
        preview = preview_missing_paid_prepaid_invoice_repair(setup, query)
        command = CreateReviewedPaidPrepaidInvoiceCommand(
            context=CommandContext.system(
                actor="pytest:billing-operator",
                scope="prepaid_draft_reconciliation",
                reason="Concurrent reviewed missing-invoice repair",
                idempotency_key=f"pg-missing-invoice-repair-{suffix}",
            ),
            query=query,
            preview_fingerprint=preview.fingerprint,
        )
        setup.commit()

    barrier = Barrier(2)

    def reconcile() -> tuple[uuid.UUID, bool]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            result = create_reviewed_paid_prepaid_invoice(worker, command)
            return result.invoice_id, result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reconcile(), range(2)))

    assert len({invoice_id for invoice_id, _replayed in results}) == 1
    assert sorted(replayed for _invoice_id, replayed in results) == [False, True]
    with session_factory() as check:
        invoices = (
            check.query(Invoice)
            .filter(Invoice.account_id == account.id)
            .filter(Invoice.is_active.is_(True))
            .all()
        )
        assert len(invoices) == 1
        assert invoices[0].status is InvoiceStatus.paid
        assert invoices[0].balance_due == Decimal("0.00")


def test_concurrent_legacy_payment_backed_repairs_converge_on_one_invoice(
    engine,
) -> None:
    """PostgreSQL locks and idempotency serialize the append-only repair."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    period_start = datetime(2026, 7, 27, tzinfo=UTC)
    period_end = datetime(2026, 8, 27, tzinfo=UTC)
    paid_at = period_start - timedelta(days=1)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Legacy Repair {suffix}",
            code=f"legacy-repair-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Synthetic",
            last_name="LegacyRepair",
            email=f"legacy-repair-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Legacy Repair Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
        )
        tax_rate = TaxRate(
            name=f"Synthetic VAT {suffix}",
            code=f"VAT-{suffix}",
            rate=Decimal("7.5000"),
            is_active=True,
        )
        setup.add_all([reseller, account, offer, tax_rate])
        setup.flush()
        account.tax_rate_id = tax_rate.id
        setup.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("100.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                tax_application=TaxApplication.exclusive,
                is_active=True,
            )
        )
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            unit_price=Decimal("100.00"),
            start_at=period_start - timedelta(days=31),
            next_billing_at=period_end,
        )
        setup.add(subscription)
        setup.flush()
        payment = Payment(
            account_id=account.id,
            amount=Decimal("107.50"),
            refunded_amount=Decimal("0.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=paid_at,
            is_active=True,
            created_at=paid_at,
        )
        setup.add(payment)
        setup.flush()
        credit = LedgerEntry(
            account_id=account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("107.50"),
            currency="NGN",
            memo="Synthetic settled credit",
            is_active=True,
            affects_customer_position=False,
            effective_date=paid_at,
            created_at=paid_at,
        )
        setup.add(credit)
        setup.flush()
        setup.add(
            PaymentSettlement(
                payment_id=payment.id,
                unallocated_ledger_entry_id=credit.id,
                amount=Decimal("107.50"),
                unallocated_amount=Decimal("107.50"),
                prepaid_amount=Decimal("107.50"),
                currency="NGN",
                origin=PaymentSettlementOrigin.system,
                idempotency_key=f"pg-legacy-repair-settlement-{suffix}",
                created_at=paid_at,
            )
        )
        debit = LedgerEntry(
            account_id=account.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.invoice,
            category=LedgerCategory.internet_service,
            amount=Decimal("107.50"),
            currency="NGN",
            memo="Synthetic legacy prepaid renewal",
            is_active=True,
            affects_customer_position=True,
            effective_date=period_start,
            created_at=period_start,
        )
        setup.add(debit)
        setup.flush()
        entitlement = ServiceEntitlement(
            account_id=account.id,
            subscription_id=subscription.id,
            source_ledger_entry_id=debit.id,
            starts_at=period_start,
            ends_at=period_end,
            amount_funded=Decimal("107.50"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
            metadata_={"source": "wallet_prepaid_renewal"},
        )
        setup.add(entitlement)
        setup.flush()
        stage_prepaid_service_renewed_outcome(
            setup,
            account_id=account.id,
            subscription_id=subscription.id,
            entitlement_id=entitlement.id,
            invoice_id=None,
            ledger_entry_id=debit.id,
            period_start=period_start,
            renewed_through=period_end,
            amount=Decimal("107.50"),
            currency="NGN",
            source=PrepaidServiceRenewalSource.direct_payment,
            trigger_payment_id=payment.id,
        )
        setup.commit()
        query = LegacyPaymentBackedRenewalInvoiceRepairQuery(
            account_id=account.id,
            subscription_id=subscription.id,
            payment_id=payment.id,
            ledger_entry_id=debit.id,
            entitlement_id=entitlement.id,
            expected_period_start=period_start,
            expected_period_end=period_end,
            expected_gross_total=Decimal("107.50"),
            expected_currency="NGN",
            expected_available_funding_after_repair=Decimal("0.00"),
        )
        preview = preview_legacy_payment_backed_renewal_invoice_repair(setup, query)
        assert preview.actionable
        actor_id = uuid.uuid4()
        command = RepairLegacyPaymentBackedRenewalInvoiceCommand(
            context=CommandContext.system(
                actor=f"user:{actor_id}",
                scope="billing:ledger:write",
                reason="SYNTHETIC-PG-APPROVAL",
                idempotency_key=f"pg-legacy-payment-backed-repair-{suffix}",
            ),
            query=query,
            preview_fingerprint=preview.fingerprint,
            actor_user_id=actor_id,
            permission_granted=True,
            permission_evidence="SYNTHETIC-PG-PERMISSION",
        )
        setup.commit()
        account_id = account.id
        debit_id = debit.id

    barrier = Barrier(2)

    def repair() -> tuple[uuid.UUID, bool]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            result = repair_legacy_payment_backed_renewal_invoice(worker, command)
            return result.invoice_id, result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: repair(), range(2)))

    assert len({invoice_id for invoice_id, _replayed in results}) == 1
    assert sorted(replayed for _invoice_id, replayed in results) == [False, True]
    with session_factory() as check:
        invoices = check.query(Invoice).filter(Invoice.account_id == account_id).all()
        assert len(invoices) == 1
        assert invoices[0].status is InvoiceStatus.paid
        assert (
            check.query(LedgerEntry)
            .filter(LedgerEntry.reversal_of_entry_id == debit_id)
            .count()
            == 1
        )
        assert (
            check.query(ServiceEntitlement)
            .filter(
                ServiceEntitlement.subscription_id == query.subscription_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .count()
            == 1
        )
