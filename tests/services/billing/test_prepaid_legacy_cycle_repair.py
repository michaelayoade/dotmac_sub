from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentPrepaidApplication,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
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
from app.services.billing import payments
from app.services.billing._common import get_account_credit_balance
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)


def _repair_evidence(db, subscriber) -> dict[str, object]:
    offer = CatalogOffer(
        name=f"Prepaid repair {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    db.add(
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
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        unit_price=Decimal("100.00"),
        next_billing_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    db.add(subscription)
    db.flush()

    historical_invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="HIST-PAID",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=datetime(2026, 6, 10, tzinfo=UTC),
        billing_period_end=datetime(2026, 7, 9, tzinfo=UTC),
        paid_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    draft_invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="NEXT-DRAFT",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        billing_period_start=datetime(2026, 7, 10, tzinfo=UTC),
        billing_period_end=datetime(2026, 8, 10, tzinfo=UTC),
    )
    db.add_all([historical_invoice, draft_invoice])
    db.flush()
    db.add_all(
        [
            InvoiceLine(
                invoice_id=historical_invoice.id,
                subscription_id=subscription.id,
                description="June prepaid service",
                amount=Decimal("100.00"),
                unit_price=Decimal("100.00"),
                is_active=True,
            ),
            InvoiceLine(
                invoice_id=draft_invoice.id,
                subscription_id=subscription.id,
                description="July prepaid service",
                amount=Decimal("100.00"),
                unit_price=Decimal("100.00"),
                is_active=True,
            ),
        ]
    )

    historical_payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )
    renewal_payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime(2026, 7, 18, 10, 30, tzinfo=UTC),
    )
    db.add_all([historical_payment, renewal_payment])
    db.flush()
    historical_allocation = PaymentAllocation(
        payment_id=historical_payment.id,
        invoice_id=historical_invoice.id,
        amount=Decimal("100.00"),
        is_active=True,
    )
    historical_debit = LedgerEntry(
        account_id=subscriber.id,
        payment_id=historical_payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        category=LedgerCategory.internet_service,
        amount=Decimal("100.00"),
        currency="NGN",
        effective_date=datetime(2026, 6, 10, tzinfo=UTC),
        memo="Manual prepaid service renewal 2026-06-10 - 2026-07-10",
    )
    renewal_credit = LedgerEntry(
        account_id=subscriber.id,
        payment_id=renewal_payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("100.00"),
        currency="NGN",
        effective_date=datetime(2026, 7, 18, 10, 30, tzinfo=UTC),
    )
    db.add_all([historical_allocation, historical_debit, renewal_credit])
    db.flush()
    renewal_settlement = PaymentSettlement(
        payment_id=renewal_payment.id,
        unallocated_ledger_entry_id=renewal_credit.id,
        amount=Decimal("100.00"),
        unallocated_amount=Decimal("100.00"),
        prepaid_amount=Decimal("0.00"),
        currency="NGN",
        origin=PaymentSettlementOrigin.manual,
        idempotency_key=f"test-renewal-{renewal_payment.id}",
    )
    db.add(renewal_settlement)
    db.commit()
    return {
        "historical_payment_id": str(historical_payment.id),
        "historical_allocation_id": str(historical_allocation.id),
        "historical_invoice_id": str(historical_invoice.id),
        "historical_debit_ledger_entry_id": str(historical_debit.id),
        "renewal_payment_id": str(renewal_payment.id),
        "draft_invoice_id": str(draft_invoice.id),
        "subscription_id": str(subscription.id),
    }


def test_repair_reconstructs_history_funds_next_cycle_and_voids_draft(
    db_session, subscriber
):
    selected = _repair_evidence(db_session, subscriber)
    preview = payments.preview_prepaid_legacy_cycle_repair(db_session, **selected)

    assert preview.account_credit_before == Decimal("0.00")
    assert preview.account_credit_after_historical_repair == Decimal("100.00")
    assert preview.account_credit_after_renewal == Decimal("0.00")
    assert preview.historical_period_end.date().isoformat() == "2026-07-10"
    assert preview.renewal_period_start.date().isoformat() == "2026-07-18"
    assert preview.renewal_period_end.date().isoformat() == "2026-08-18"
    assert db_session.query(PaymentPrepaidApplication).count() == 0

    result = payments.confirm_prepaid_legacy_cycle_repair(
        db_session,
        **selected,
        preview_fingerprint=preview.fingerprint,
        idempotency_key="test-prepaid-cycle-repair-0001",
        reason="Reviewed exact legacy payment, invoice, debit, and renewal credit",
    )

    allocation = db_session.get(
        PaymentAllocation, result.historical_application.retired_allocation_id
    )
    historical_payment = db_session.get(Payment, preview.historical_payment_id)
    draft_invoice = db_session.get(Invoice, preview.draft_invoice_id)
    subscription = db_session.get(Subscription, preview.subscription_id)
    assert allocation is not None and allocation.is_active is False
    assert historical_payment is not None and historical_payment.settlement is not None
    assert historical_payment.settlement.prepaid_amount == Decimal("100.00")
    assert draft_invoice is not None and draft_invoice.status == InvoiceStatus.void
    assert subscription is not None
    assert subscription.next_billing_at.date().isoformat() == "2026-08-18"
    assert db_session.query(PaymentPrepaidApplication).count() == 2
    assert db_session.query(ServiceEntitlement).count() == 2
    assert get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    ) == Decimal("0.00")

    replay = payments.confirm_prepaid_legacy_cycle_repair(
        db_session,
        **selected,
        preview_fingerprint=preview.fingerprint,
        idempotency_key="test-prepaid-cycle-repair-0001",
        reason="Reviewed exact legacy payment, invoice, debit, and renewal credit",
    )
    assert replay.idempotent_replay is True
    assert db_session.query(PaymentPrepaidApplication).count() == 2


def test_access_failure_is_deferred_after_financial_commit(
    db_session, subscriber, monkeypatch
):
    selected = _repair_evidence(db_session, subscriber)
    preview = payments.preview_prepaid_legacy_cycle_repair(db_session, **selected)
    result = payments.confirm_prepaid_legacy_cycle_repair(
        db_session,
        **selected,
        preview_fingerprint=preview.fingerprint,
        idempotency_key="test-prepaid-cycle-repair-0002",
        reason="Reviewed exact legacy payment, invoice, debit, and renewal credit",
    )

    from app.services import collections as collections_service

    def missing_baseline(*_args, **_kwargs):
        raise PrepaidFundingBaselineMissingError("baseline unavailable")

    monkeypatch.setattr(
        collections_service, "restore_account_services", missing_baseline
    )
    application = payments.recheck_prepaid_application_access(
        db_session, str(result.renewal_application.id)
    )

    assert application.access_recheck_status == "deferred"
    assert application.access_recheck_error == "PrepaidFundingBaselineMissingError"
    assert (
        db_session.get(Invoice, preview.draft_invoice_id).status == InvoiceStatus.void
    )
    assert db_session.query(PaymentPrepaidApplication).count() == 2
