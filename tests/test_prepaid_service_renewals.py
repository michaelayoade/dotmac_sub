from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    PaymentReversalOrigin,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    Subscription,
    SubscriptionStatus,
    UsageAllowance,
    UsageAllowanceResetBasis,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.access_resolution import PrepaidFundingDecision
from app.services.billing._common import get_account_credit_balance
from app.services.billing.invoices import InvoiceOwnerError, Invoices
from app.services.billing.payments import payment_application_summary
from app.services.customer_financial_ledger import calculate_customer_balance
from app.services.customer_portal_flow_billing import (
    CustomerInvoiceHistoryQuery,
    _get_customer_invoice_history,
)
from app.services.domain_errors import DomainError
from app.services.events.handlers.prepaid_renewal import PrepaidRenewalHandler
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.prepaid_calendar_contracts import (
    DocumentedPrepaidServicePeriod,
    ReviewedPrepaidCalendarBasis,
    ReviewedPrepaidServicePeriodQuery,
)
from app.services.prepaid_service_renewals import (
    CorrectLegacyRenewalTaxInvoiceCommand,
    CorrectTaxInclusiveRenewalDocumentCommand,
    ExecuteReviewedPrepaidServiceRenewalCommand,
    FundingChangeRenewalDisposition,
    LegacyPaymentBackedRenewalInvoiceRepairDisposition,
    LegacyPaymentBackedRenewalInvoiceRepairQuery,
    LegacyRenewalTaxInvoiceCorrectionDisposition,
    LegacyRenewalTaxInvoiceCorrectionQuery,
    PrepaidServiceRenewalError,
    PrepaidServiceRenewalSource,
    PrepaidSettlementPeriodQuery,
    PrepaidSubscriptionSettlementPeriodQuery,
    RepairLegacyPaymentBackedRenewalInvoiceCommand,
    RunDuePrepaidServiceRenewalsCommand,
    TaxInclusiveRenewalDocumentCorrectionDisposition,
    TaxInclusiveRenewalDocumentCorrectionQuery,
    apply_due_prepaid_service_after_funding_change,
    confirm_prepaid_service_renewal,
    correct_legacy_prepaid_renewal_tax_invoice,
    correct_tax_inclusive_renewal_document,
    execute_due_prepaid_service_renewals,
    execute_reviewed_prepaid_service_renewal,
    preview_legacy_payment_backed_renewal_invoice_repair,
    preview_legacy_prepaid_renewal_tax_invoice_correction,
    preview_prepaid_service_renewal,
    preview_tax_inclusive_renewal_document_correction,
    repair_legacy_payment_backed_renewal_invoice,
    resolve_prepaid_monthly_charge_detail,
    resolve_prepaid_settlement_period,
    resolve_prepaid_subscription_settlement_period,
    resolve_reviewed_prepaid_service_period,
    run_due_prepaid_service_renewals,
    stage_prepaid_service_renewed_outcome,
)
from tests.prepaid_funding_helpers import (
    create_test_settled_payment_credit,
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)


def test_settlement_period_uses_lagos_calendar_day_before_month_arithmetic():
    period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            # 00:30 on July 6 in Lagos is still July 5 in UTC.
            effective_at=datetime(2026, 7, 5, 23, 30, tzinfo=UTC),
            billing_cycle=BillingCycle.monthly,
        )
    )

    assert period.starts_on.isoformat() == "2026-07-06"
    assert period.ends_on.isoformat() == "2026-08-06"
    assert period.starts_at == datetime(2026, 7, 5, 23, tzinfo=UTC)
    assert period.ends_at == datetime(2026, 8, 5, 23, tzinfo=UTC)
    assert period.timezone_name == "Africa/Lagos"


def test_settlement_period_preserves_declared_month_end_clamp():
    period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=datetime(2026, 1, 31, 23, 30, tzinfo=UTC),
            billing_cycle=BillingCycle.monthly,
        )
    )

    assert period.starts_on.isoformat() == "2026-02-01"
    assert period.ends_on.isoformat() == "2026-03-01"
    assert period.starts_at == datetime(2026, 1, 31, 23, tzinfo=UTC)
    assert period.ends_at == datetime(2026, 2, 28, 23, tzinfo=UTC)


def test_reviewed_dates_default_to_lagos_midnight():
    period = resolve_reviewed_prepaid_service_period(
        ReviewedPrepaidServicePeriodQuery(date(2026, 6, 29), date(2026, 7, 29))
    )
    assert period.starts_at == datetime(2026, 6, 28, 23, tzinfo=UTC)
    assert period.ends_at == datetime(2026, 7, 28, 23, tzinfo=UTC)
    assert period.timezone_name == "Africa/Lagos"


def test_reviewed_documentary_continuation_preserves_utc_instants():
    period = resolve_reviewed_prepaid_service_period(
        ReviewedPrepaidServicePeriodQuery(
            starts_on=date(2026, 7, 29),
            ends_on=date(2026, 8, 28),
            basis=ReviewedPrepaidCalendarBasis.documented_anniversary,
            documented_period=DocumentedPrepaidServicePeriod(
                datetime(2026, 6, 29, tzinfo=UTC),
                datetime(2026, 7, 29, tzinfo=UTC),
            ),
        )
    )
    assert period.starts_at == datetime(2026, 7, 29, tzinfo=UTC)
    assert period.ends_at == datetime(2026, 8, 28, tzinfo=UTC)
    assert period.starts_on == date(2026, 7, 29)
    assert period.ends_on == date(2026, 8, 28)


@pytest.mark.parametrize(
    "reference",
    [
        None,
        DocumentedPrepaidServicePeriod(datetime(2026, 6, 29), datetime(2026, 7, 29)),
        DocumentedPrepaidServicePeriod(
            datetime(2026, 6, 29, tzinfo=UTC), datetime(2026, 7, 29, 1, tzinfo=UTC)
        ),
        DocumentedPrepaidServicePeriod(
            datetime(2026, 7, 29, tzinfo=UTC), datetime(2026, 6, 29, tzinfo=UTC)
        ),
    ],
)
def test_reviewed_documentary_calendar_requires_exact_reference(
    reference: DocumentedPrepaidServicePeriod | None,
):
    with pytest.raises(PrepaidServiceRenewalError) as exc:
        resolve_reviewed_prepaid_service_period(
            ReviewedPrepaidServicePeriodQuery(
                date(2026, 7, 29),
                date(2026, 8, 28),
                ReviewedPrepaidCalendarBasis.documented_anniversary,
                reference,
            )
        )
    assert exc.value.code == "financial.prepaid_service_renewals.invalid_period"


def test_business_midnight_cannot_silently_accept_documentary_override():
    with pytest.raises(PrepaidServiceRenewalError):
        resolve_reviewed_prepaid_service_period(
            ReviewedPrepaidServicePeriodQuery(
                date(2026, 7, 29),
                date(2026, 8, 28),
                documented_period=DocumentedPrepaidServicePeriod(
                    datetime(2026, 6, 29, tzinfo=UTC), datetime(2026, 7, 29, tzinfo=UTC)
                ),
            )
        )


def test_capped_renewal_restarts_validity_even_with_live_coverage(
    db_session, subscriber, subscription
):
    allowance = UsageAllowance(
        name="Capped 100GB / 30 days",
        included_gb=100,
        reset_basis=UsageAllowanceResetBasis.renewal_cycle,
        validity_days=30,
        is_active=True,
    )
    db_session.add(allowance)
    db_session.flush()
    subscription.offer.usage_allowance_id = allowance.id
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    effective_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    existing_end = datetime(2026, 9, 25, 23, tzinfo=UTC)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=datetime(2026, 8, 25, 23, tzinfo=UTC),
            ends_at=existing_end,
            amount_funded=Decimal("1.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
        )
    )
    db_session.flush()

    decision = resolve_prepaid_subscription_settlement_period(
        db_session,
        PrepaidSubscriptionSettlementPeriodQuery(
            subscription_id=subscription.id,
            account_id=subscriber.id,
            effective_at=effective_at,
            billing_cycle=BillingCycle.monthly,
        ),
    )

    assert decision.covered_through == existing_end
    assert decision.period.starts_at == effective_at
    assert decision.period.ends_at == datetime(2026, 10, 14, 12, tzinfo=UTC)


def _legacy_tax_correction_fixture(db_session, subscriber, subscription):
    period_start = datetime(2026, 8, 22, 8, 59, tzinfo=UTC)
    period_end = datetime(2026, 9, 22, 8, 59, tzinfo=UTC)
    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = period_end
    ensure_test_prepaid_contract(db_session, subscription, Decimal("70000.00"))
    tax_rate = TaxRate(
        name="Legacy renewal VAT",
        code="VAT-LEGACY-RENEWAL",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    subscriber.tax_rate_id = tax_rate.id
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("0.00"),
        position_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    payment = create_test_settled_payment_credit(
        db_session,
        subscriber.id,
        Decimal("75250.50"),
        paid_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    legacy_ledger = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.adjustment,
        category=LedgerCategory.internet_service,
        amount=Decimal("70000.00"),
        currency="NGN",
        memo="Legacy prepaid service renewal",
        effective_date=period_start,
        created_at=period_start,
        is_active=True,
        affects_customer_position=True,
    )
    db_session.add(legacy_ledger)
    db_session.flush()
    legacy_adjustment = AccountAdjustment(
        account_id=subscriber.id,
        category=LedgerCategory.internet_service,
        amount=Decimal("70000.00"),
        currency="NGN",
        memo="Legacy prepaid service renewal",
        reason="Historical direct renewal",
        origin="prepaid_service_renewal",
        origin_ref=(
            f"{subscription.id}:{period_start.isoformat()}:{period_end.isoformat()}"
        ),
        prepaid_funding_before=Decimal("75250.50"),
        prepaid_funding_after=Decimal("5250.50"),
        postpaid_receivables=Decimal("0.00"),
        collection_blocking_balance=Decimal("0.00"),
        access_consequence="none_adjustment_only",
        preview_fingerprint="a" * 64,
        idempotency_key=f"pytest-legacy-renewal:{subscription.id}",
        ledger_entry_id=legacy_ledger.id,
        created_at=period_start,
    )
    legacy_entitlement = ServiceEntitlement(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        source_ledger_entry_id=legacy_ledger.id,
        starts_at=period_start,
        ends_at=period_end,
        amount_funded=Decimal("70000.00"),
        currency="NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={"origin": "prepaid_service_renewal"},
        created_at=period_start,
    )
    db_session.add_all([legacy_adjustment, legacy_entitlement])
    db_session.commit()
    return (
        period_start,
        period_end,
        payment,
        legacy_adjustment,
        legacy_entitlement,
    )


def test_legacy_base_only_renewal_is_atomically_replaced_by_taxed_paid_invoice(
    db_session, subscriber, subscription
):
    (
        period_start,
        period_end,
        payment,
        legacy_adjustment,
        legacy_entitlement,
    ) = _legacy_tax_correction_fixture(db_session, subscriber, subscription)
    query = LegacyRenewalTaxInvoiceCorrectionQuery(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        adjustment_id=legacy_adjustment.id,
        entitlement_id=legacy_entitlement.id,
        expected_invoice_total=Decimal("75250.00"),
        expected_remaining_credit=Decimal("0.50"),
    )
    preview = preview_legacy_prepaid_renewal_tax_invoice_correction(db_session, query)

    assert (
        preview.disposition
        is LegacyRenewalTaxInvoiceCorrectionDisposition.exact_base_only_legacy_renewal
    )
    assert preview.original_debit == Decimal("70000.00")
    assert preview.tax_total == Decimal("5250.00")
    assert preview.tax_rate_id is not None
    assert preview.tax_application.value == "exclusive"
    assert preview.invoice_total == Decimal("75250.00")
    assert preview.credit_before == Decimal("5250.50")
    assert preview.credit_after == Decimal("0.50")
    db_session.rollback()
    command = CorrectLegacyRenewalTaxInvoiceCommand(
        context=CommandContext.system(
            actor=f"user:{query.account_id}",
            scope="billing:ledger:write",
            reason="Finance-approved historical renewal invoice correction",
            idempotency_key=f"pytest-legacy-tax-correction:{query.subscription_id}",
        ),
        query=query,
        expected_preview_fingerprint=preview.fingerprint,
    )
    result = correct_legacy_prepaid_renewal_tax_invoice(db_session, command)

    invoice = db_session.get(Invoice, result.invoice_id)
    db_session.refresh(legacy_adjustment)
    db_session.refresh(legacy_entitlement)
    replacement = db_session.get(ServiceEntitlement, result.replacement_entitlement_id)
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == result.invoice_id)
        .all()
    )
    assert invoice is not None
    assert replacement is not None
    assert invoice.status is InvoiceStatus.paid
    assert invoice.subtotal == Decimal("70000.00")
    assert invoice.tax_total == Decimal("5250.00")
    assert invoice.total == Decimal("75250.00")
    assert invoice.balance_due == Decimal("0.00")
    assert legacy_adjustment.reversal_ledger_entry_id == result.reversal_ledger_entry_id
    assert legacy_adjustment.reversed_at is not None
    assert legacy_entitlement.status is ServiceEntitlementStatus.reversed
    assert replacement.status is ServiceEntitlementStatus.active
    assert replacement.source_invoice_id == invoice.id
    assert replacement.starts_at.replace(tzinfo=UTC) == period_start
    assert replacement.ends_at.replace(tzinfo=UTC) == period_end
    assert sum((row.amount for row in allocations), Decimal("0.00")) == Decimal(
        "75250.00"
    )
    assert {row.payment_id for row in allocations} == {payment.id}
    assert result.remaining_credit == Decimal("0.50")
    assert subscription.next_billing_at.replace(tzinfo=UTC) == period_end
    correction_events = (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type
            == EventType.prepaid_service_renewal_document_corrected.value
        )
        .all()
    )
    assert len(correction_events) == 1

    db_session.rollback()
    replay = correct_legacy_prepaid_renewal_tax_invoice(db_session, command)
    assert replay.replayed is True
    assert replay.invoice_id == result.invoice_id
    assert (
        db_session.query(Invoice).filter(Invoice.account_id == subscriber.id).count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type
            == EventType.prepaid_service_renewal_document_corrected.value
        )
        .count()
        == 1
    )


def test_legacy_tax_correction_refuses_balance_drift_without_writes(
    db_session, subscriber, subscription
):
    (
        _period_start,
        _period_end,
        _payment,
        legacy_adjustment,
        legacy_entitlement,
    ) = _legacy_tax_correction_fixture(db_session, subscriber, subscription)
    query = LegacyRenewalTaxInvoiceCorrectionQuery(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        adjustment_id=legacy_adjustment.id,
        entitlement_id=legacy_entitlement.id,
        expected_invoice_total=Decimal("75250.00"),
        expected_remaining_credit=Decimal("1.00"),
    )

    preview = preview_legacy_prepaid_renewal_tax_invoice_correction(db_session, query)

    assert (
        preview.disposition
        is LegacyRenewalTaxInvoiceCorrectionDisposition.manual_review
    )
    assert db_session.query(Invoice).count() == 0
    db_session.refresh(legacy_adjustment)
    db_session.refresh(legacy_entitlement)
    assert legacy_adjustment.reversed_at is None
    assert legacy_entitlement.status is ServiceEntitlementStatus.active


def test_legacy_tax_correction_rolls_back_reversal_when_invoice_owner_rejects(
    db_session, subscriber, subscription, monkeypatch
):
    (
        _period_start,
        _period_end,
        _payment,
        legacy_adjustment,
        legacy_entitlement,
    ) = _legacy_tax_correction_fixture(db_session, subscriber, subscription)
    query = LegacyRenewalTaxInvoiceCorrectionQuery(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        adjustment_id=legacy_adjustment.id,
        entitlement_id=legacy_entitlement.id,
        expected_invoice_total=Decimal("75250.00"),
        expected_remaining_credit=Decimal("0.50"),
    )
    preview = preview_legacy_prepaid_renewal_tax_invoice_correction(db_session, query)
    adjustment_id = legacy_adjustment.id
    entitlement_id = legacy_entitlement.id
    db_session.rollback()

    def reject_invoice(*_args, **_kwargs):
        raise InvoiceOwnerError(
            code="financial.invoice.pytest_rejection",
            message="pytest invoice rejection",
        )

    monkeypatch.setattr(Invoices, "stage_system_invoice_for_owner", reject_invoice)
    with pytest.raises(PrepaidServiceRenewalError) as exc_info:
        correct_legacy_prepaid_renewal_tax_invoice(
            db_session,
            CorrectLegacyRenewalTaxInvoiceCommand(
                context=CommandContext.system(
                    actor=f"user:{query.account_id}",
                    scope="billing:ledger:write",
                    reason="Finance-approved historical renewal invoice correction",
                    idempotency_key=(
                        f"pytest-legacy-tax-correction-rollback:{query.subscription_id}"
                    ),
                ),
                query=query,
                expected_preview_fingerprint=preview.fingerprint,
            ),
        )

    assert exc_info.value.code.endswith("invoice_rejected")
    adjustment = db_session.get(AccountAdjustment, adjustment_id)
    entitlement = db_session.get(ServiceEntitlement, entitlement_id)
    assert adjustment is not None
    assert entitlement is not None
    assert adjustment.reversed_at is None
    assert adjustment.reversal_ledger_entry_id is None
    assert entitlement.status is ServiceEntitlementStatus.active
    assert db_session.query(Invoice).count() == 0
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type
            == EventType.prepaid_service_renewal_document_corrected.value
        )
        .count()
        == 0
    )


def test_tax_inclusive_renewal_document_correction_is_zero_delta_and_replayable(
    db_session, subscriber, subscription
):
    (
        period_start,
        period_end,
        payment,
        adjustment,
        entitlement,
    ) = _legacy_tax_correction_fixture(db_session, subscriber, subscription)
    ledger_entry = db_session.get(LedgerEntry, adjustment.ledger_entry_id)
    assert ledger_entry is not None
    adjustment.amount = Decimal("75250.00")
    adjustment.prepaid_funding_before = Decimal("75250.50")
    adjustment.prepaid_funding_after = Decimal("0.50")
    ledger_entry.amount = Decimal("75250.00")
    entitlement.amount_funded = Decimal("75250.00")
    subscription.status = SubscriptionStatus.suspended
    db_session.commit()
    query = TaxInclusiveRenewalDocumentCorrectionQuery(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        adjustment_id=adjustment.id,
        entitlement_id=entitlement.id,
        payment_id=payment.id,
        expected_invoice_total=Decimal("75250.00"),
        expected_remaining_credit=Decimal("0.50"),
    )

    base_only = preview_legacy_prepaid_renewal_tax_invoice_correction(
        db_session,
        LegacyRenewalTaxInvoiceCorrectionQuery(
            account_id=query.account_id,
            subscription_id=query.subscription_id,
            adjustment_id=query.adjustment_id,
            entitlement_id=query.entitlement_id,
            expected_invoice_total=query.expected_invoice_total,
            expected_remaining_credit=query.expected_remaining_credit,
        ),
    )
    preview = preview_tax_inclusive_renewal_document_correction(db_session, query)

    assert (
        base_only.disposition
        is LegacyRenewalTaxInvoiceCorrectionDisposition.manual_review
    )
    assert preview.disposition is (
        TaxInclusiveRenewalDocumentCorrectionDisposition.exact_tax_inclusive_renewal
    )
    assert preview.credit_before == Decimal("0.50")
    assert preview.temporary_credit_after_reversal == Decimal("75250.50")
    command = CorrectTaxInclusiveRenewalDocumentCommand(
        context=CommandContext.system(
            actor=f"user:{subscriber.id}",
            scope="billing:ledger:write",
            reason="FIN-TEST reviewed documentary conversion",
            idempotency_key=f"pytest-tax-inclusive-document:{subscription.id}",
        ),
        query=query,
        preview_fingerprint=preview.fingerprint,
        permission_evidence="pytest active billing:ledger:write role",
    )
    db_session.rollback()
    result = correct_tax_inclusive_renewal_document(db_session, command)
    replay = correct_tax_inclusive_renewal_document(db_session, command)

    invoice = db_session.get(Invoice, result.invoice_id)
    replacement = db_session.get(ServiceEntitlement, result.replacement_entitlement_id)
    db_session.refresh(subscription)
    assert invoice is not None
    assert replacement is not None
    assert invoice.status is InvoiceStatus.paid
    assert invoice.subtotal == Decimal("70000.00")
    assert invoice.tax_total == Decimal("5250.00")
    assert invoice.total == Decimal("75250.00")
    assert result.remaining_credit == Decimal("0.50")
    assert replacement.starts_at.replace(tzinfo=UTC) == period_start
    assert replacement.ends_at.replace(tzinfo=UTC) == period_end
    assert subscription.status is SubscriptionStatus.suspended
    assert subscription.next_billing_at.replace(tzinfo=UTC) == period_end
    history = _get_customer_invoice_history(
        db_session,
        CustomerInvoiceHistoryQuery(
            account_id=subscriber.id,
            status=None,
            page=1,
            per_page=20,
        ),
    )
    assert invoice.id in {item.id for item in history.invoices}
    assert replay.replayed is True
    assert replay.invoice_id == result.invoice_id


def test_tax_inclusive_document_correction_rejects_prepaid_suspension_lock(
    db_session, subscriber, subscription
):
    _, _, payment, adjustment, entitlement = _legacy_tax_correction_fixture(
        db_session, subscriber, subscription
    )
    ledger_entry = db_session.get(LedgerEntry, adjustment.ledger_entry_id)
    assert ledger_entry is not None
    adjustment.amount = Decimal("75250.00")
    adjustment.prepaid_funding_before = Decimal("75250.50")
    adjustment.prepaid_funding_after = Decimal("0.50")
    ledger_entry.amount = Decimal("75250.00")
    entitlement.amount_funded = Decimal("75250.00")
    subscription.status = SubscriptionStatus.suspended
    db_session.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.prepaid,
            source="pytest:prepaid-lock",
            is_active=True,
        )
    )
    db_session.commit()

    preview = preview_tax_inclusive_renewal_document_correction(
        db_session,
        TaxInclusiveRenewalDocumentCorrectionQuery(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            adjustment_id=adjustment.id,
            entitlement_id=entitlement.id,
            payment_id=payment.id,
            expected_invoice_total=Decimal("75250.00"),
            expected_remaining_credit=Decimal("0.50"),
        ),
    )

    assert preview.disposition is (
        TaxInclusiveRenewalDocumentCorrectionDisposition.manual_review
    )
    assert db_session.query(Invoice).count() == 0


def _legacy_payment_backed_invoice_repair_fixture(db_session, subscriber, subscription):
    period_start = datetime(2026, 7, 27, tzinfo=UTC)
    period_end = datetime(2026, 8, 27, tzinfo=UTC)
    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = period_end
    ensure_test_prepaid_contract(db_session, subscription, Decimal("35000.00"))
    tax_rate = TaxRate(
        name="Synthetic renewal VAT",
        code=f"VAT-SYNTH-{uuid4()}",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    subscriber.tax_rate_id = tax_rate.id
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("0.00"),
        position_at=period_start - timedelta(days=2),
    )
    payment = create_test_settled_payment_credit(
        db_session,
        subscriber.id,
        Decimal("37625.00"),
        paid_at=period_start - timedelta(days=1),
    )
    payment.settlement.prepaid_amount = Decimal("37625.00")
    legacy_ledger = LedgerEntry(
        account_id=subscriber.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        category=LedgerCategory.internet_service,
        amount=Decimal("37625.00"),
        currency="NGN",
        memo="Synthetic legacy wallet prepaid renewal",
        effective_date=period_start,
        created_at=period_start,
        is_active=True,
        affects_customer_position=True,
    )
    db_session.add(legacy_ledger)
    db_session.flush()
    entitlement = ServiceEntitlement(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        source_ledger_entry_id=legacy_ledger.id,
        starts_at=period_start,
        ends_at=period_end,
        amount_funded=Decimal("37625.00"),
        currency="NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={
            "source": "wallet_prepaid_renewal",
            "source_ledger_entry_id": str(legacy_ledger.id),
        },
        created_at=period_start,
    )
    db_session.add(entitlement)
    db_session.flush()
    stage_prepaid_service_renewed_outcome(
        db_session,
        account_id=subscriber.id,
        subscription_id=subscription.id,
        entitlement_id=entitlement.id,
        invoice_id=None,
        ledger_entry_id=legacy_ledger.id,
        period_start=period_start,
        renewed_through=period_end,
        amount=Decimal("37625.00"),
        currency="NGN",
        source=PrepaidServiceRenewalSource.direct_payment,
        trigger_payment_id=payment.id,
    )
    db_session.commit()
    query = LegacyPaymentBackedRenewalInvoiceRepairQuery(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        payment_id=payment.id,
        ledger_entry_id=legacy_ledger.id,
        entitlement_id=entitlement.id,
        expected_period_start=period_start,
        expected_period_end=period_end,
        expected_gross_total=Decimal("37625.00"),
        expected_currency="NGN",
        expected_available_funding_after_repair=Decimal("0.00"),
    )
    return query, payment, legacy_ledger, entitlement


def _legacy_payment_backed_repair_command(query, fingerprint):
    actor_user_id = uuid4()
    return RepairLegacyPaymentBackedRenewalInvoiceCommand(
        context=CommandContext.system(
            actor=f"user:{actor_user_id}",
            scope="billing:ledger:write",
            reason="SYNTHETIC-FINANCE-EVIDENCE-001",
            idempotency_key=f"legacy-payment-renewal-repair:{query.subscription_id}",
        ),
        query=query,
        preview_fingerprint=fingerprint,
        actor_user_id=actor_user_id,
        permission_granted=True,
        permission_evidence="SYNTHETIC-APPROVAL-001",
    )


def test_legacy_payment_backed_renewal_repair_is_conservative_and_portal_visible(
    db_session, subscriber, subscription
):
    query, payment, legacy_ledger, legacy_entitlement = (
        _legacy_payment_backed_invoice_repair_fixture(
            db_session, subscriber, subscription
        )
    )
    invoices_before = db_session.query(Invoice).count()
    ledger_before = db_session.query(LedgerEntry).count()
    funding_before = get_account_credit_balance(
        db_session, str(subscriber.id), currency="NGN"
    )
    preview = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)

    assert (
        preview.disposition
        is LegacyPaymentBackedRenewalInvoiceRepairDisposition.exact_legacy_chain
    )
    assert preview.actionable is True
    assert preview.subtotal == Decimal("35000.00")
    assert preview.tax_total == Decimal("2625.00")
    assert preview.gross_total == Decimal("37625.00")
    assert preview.available_funding_before == funding_before == Decimal("0.00")
    assert preview.available_funding_after == funding_before
    assert db_session.query(Invoice).count() == invoices_before
    assert db_session.query(LedgerEntry).count() == ledger_before

    command = _legacy_payment_backed_repair_command(query, preview.fingerprint)
    db_session.rollback()
    result = repair_legacy_payment_backed_renewal_invoice(db_session, command)

    invoice = db_session.get(Invoice, result.invoice_id)
    replacement = db_session.get(ServiceEntitlement, result.replacement_entitlement_id)
    db_session.refresh(legacy_entitlement)
    db_session.refresh(subscription)
    assert invoice is not None
    assert replacement is not None
    assert invoice.is_active is True
    assert invoice.is_proforma is False
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert invoice.subtotal == Decimal("35000.00")
    assert invoice.tax_total == Decimal("2625.00")
    assert invoice.total == Decimal("37625.00")
    assert legacy_entitlement.status is ServiceEntitlementStatus.reversed
    assert replacement.status is ServiceEntitlementStatus.active
    assert replacement.source_invoice_id == invoice.id
    assert replacement.source_invoice_line_id == result.invoice_line_id
    assert replacement.starts_at.replace(tzinfo=UTC) == query.expected_period_start
    assert replacement.ends_at.replace(tzinfo=UTC) == query.expected_period_end
    assert subscription.next_billing_at.replace(tzinfo=UTC) == query.expected_period_end
    assert result.billing_anchor_before == result.billing_anchor_after
    assert result.available_funding_before == result.available_funding_after
    assert (
        get_account_credit_balance(db_session, str(subscriber.id), currency="NGN")
        == funding_before
    )
    allocation = db_session.get(PaymentAllocation, result.payment_allocation_id)
    reversal = db_session.get(LedgerEntry, result.reversal_ledger_entry_id)
    assert allocation is not None and allocation.payment_id == payment.id
    assert allocation.amount == Decimal("37625.00")
    assert reversal is not None
    assert reversal.reversal_of_entry_id == legacy_ledger.id
    payment_summary = payment_application_summary(db_session, payment)
    assert payment_summary.amount_credited == Decimal("37625.00")
    assert payment_summary.invoice_amount_applied == Decimal("37625.00")
    assert payment_summary.prepaid_amount_applied == Decimal("0.00")
    assert payment_summary.unallocated_credit == Decimal("0.00")
    history = _get_customer_invoice_history(
        db_session,
        CustomerInvoiceHistoryQuery(
            account_id=subscriber.id,
            status=None,
            page=1,
            per_page=20,
        ),
    )
    assert invoice.id in {item.id for item in history.invoices}

    counts_before_replay = (
        db_session.query(Invoice).count(),
        db_session.query(PaymentAllocation).count(),
        db_session.query(ServiceEntitlement).count(),
        db_session.query(LedgerEntry).count(),
        db_session.query(EventStore).count(),
        db_session.query(AuditEvent).count(),
    )
    db_session.rollback()
    replay = repair_legacy_payment_backed_renewal_invoice(db_session, command)
    assert replay.replayed is True
    assert replay.invoice_id == result.invoice_id
    assert replay.audit_event_id == result.audit_event_id
    assert replay.domain_event_id == result.domain_event_id
    assert counts_before_replay == (
        db_session.query(Invoice).count(),
        db_session.query(PaymentAllocation).count(),
        db_session.query(ServiceEntitlement).count(),
        db_session.query(LedgerEntry).count(),
        db_session.query(EventStore).count(),
        db_session.query(AuditEvent).count(),
    )


@pytest.mark.parametrize(
    ("drift", "value"),
    [
        ("account", None),
        ("subscription", None),
        ("payment", None),
        ("payment_status", PaymentStatus.partially_refunded),
        ("prepaid_consumption", Decimal("100.00")),
        ("ledger_active", False),
        ("ledger_amount", Decimal("37624.99")),
        ("ledger_currency", "USD"),
        ("entitlement_source", "unexpected"),
        ("anchor", datetime(2026, 8, 28, tzinfo=UTC)),
        ("period_end", datetime(2026, 8, 28, tzinfo=UTC)),
        ("gross", Decimal("37624.99")),
        ("funding", Decimal("0.01")),
        ("tax_rate", Decimal("8.0000")),
        ("contract_price", Decimal("35000.01")),
    ],
)
def test_legacy_payment_backed_repair_preview_rejects_evidence_drift(
    db_session, subscriber, subscription, drift, value
):
    query, payment, ledger, entitlement = _legacy_payment_backed_invoice_repair_fixture(
        db_session, subscriber, subscription
    )
    if drift == "account":
        query = replace(query, account_id=uuid4())
    elif drift == "subscription":
        query = replace(query, subscription_id=uuid4())
    elif drift == "payment":
        query = replace(query, payment_id=uuid4())
    elif drift == "payment_status":
        payment.status = value
        db_session.commit()
    elif drift == "prepaid_consumption":
        payment.settlement.prepaid_amount = value
        db_session.commit()
    elif drift == "ledger_active":
        ledger.is_active = value
        db_session.commit()
    elif drift == "ledger_amount":
        ledger.amount = value
        db_session.commit()
    elif drift == "ledger_currency":
        ledger.currency = value
        db_session.commit()
    elif drift == "entitlement_source":
        entitlement.metadata_ = {"source": value}
        db_session.commit()
    elif drift == "anchor":
        subscription.next_billing_at = value
        db_session.commit()
    elif drift == "period_end":
        query = replace(query, expected_period_end=value)
    elif drift == "gross":
        query = replace(query, expected_gross_total=value)
    elif drift == "funding":
        query = replace(query, expected_available_funding_after_repair=value)
    elif drift == "tax_rate":
        tax_rate = db_session.get(TaxRate, subscriber.tax_rate_id)
        assert tax_rate is not None
        tax_rate.rate = value
        db_session.commit()
    elif drift == "contract_price":
        price = db_session.scalar(
            select(OfferPrice).where(
                OfferPrice.offer_id == subscription.offer_id,
                OfferPrice.price_type == PriceType.recurring,
                OfferPrice.is_active.is_(True),
            )
        )
        assert price is not None
        price.amount = value
        subscription.unit_price = value
        db_session.commit()

    preview = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)

    assert (
        preview.disposition
        is LegacyPaymentBackedRenewalInvoiceRepairDisposition.manual_review
    )
    assert db_session.query(Invoice).count() == 0
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.reversal_of_entry_id == ledger.id)
        .count()
        == 0
    )


def test_legacy_payment_backed_repair_rejects_missing_settlement(
    db_session, subscriber, subscription
):
    query, payment, ledger, _entitlement = (
        _legacy_payment_backed_invoice_repair_fixture(
            db_session, subscriber, subscription
        )
    )
    settlement = payment.settlement
    assert settlement is not None
    db_session.delete(settlement)
    db_session.commit()
    missing = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)
    assert missing.actionable is False


def test_legacy_payment_backed_repair_rejects_payment_reversal(
    db_session, subscriber, subscription
):
    query, payment, _ledger, _entitlement = (
        _legacy_payment_backed_invoice_repair_fixture(
            db_session, subscriber, subscription
        )
    )
    db_session.add(
        PaymentReversal(
            payment_id=payment.id,
            amount=payment.amount,
            currency=payment.currency,
            origin=PaymentReversalOrigin.administrative_correction,
            reason="Synthetic reversed settlement",
        )
    )
    db_session.commit()

    preview = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)

    assert preview.actionable is False


def test_legacy_payment_backed_repair_rejects_account_adjustment_owned_debit(
    db_session, subscriber, subscription
):
    query, _payment, ledger, _entitlement = (
        _legacy_payment_backed_invoice_repair_fixture(
            db_session, subscriber, subscription
        )
    )
    db_session.add(
        AccountAdjustment(
            account_id=subscriber.id,
            category=LedgerCategory.internet_service,
            amount=ledger.amount,
            currency=ledger.currency,
            memo="Synthetic competing adjustment ownership",
            reason="Synthetic evidence-shape rejection",
            origin="prepaid_service_renewal",
            origin_ref=(
                f"{subscription.id}:{query.expected_period_start.isoformat()}:"
                f"{query.expected_period_end.isoformat()}"
            ),
            prepaid_funding_before=Decimal("37625.00"),
            prepaid_funding_after=Decimal("0.00"),
            postpaid_receivables=Decimal("0.00"),
            collection_blocking_balance=Decimal("0.00"),
            access_consequence="none_adjustment_only",
            preview_fingerprint="b" * 64,
            idempotency_key=f"synthetic-adjustment-owner:{ledger.id}",
            ledger_entry_id=ledger.id,
        )
    )
    db_session.commit()

    preview = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)

    assert preview.actionable is False


def test_legacy_payment_backed_repair_rejects_competing_invoice(
    db_session, subscriber, subscription
):
    query, _payment, _ledger, _entitlement = (
        _legacy_payment_backed_invoice_repair_fixture(
            db_session, subscriber, subscription
        )
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("35000.00"),
        tax_total=Decimal("2625.00"),
        total=Decimal("37625.00"),
        balance_due=Decimal("37625.00"),
        billing_period_start=query.expected_period_start,
        billing_period_end=query.expected_period_end,
        is_active=True,
        is_proforma=False,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Synthetic competing service invoice",
            quantity=Decimal("1.000"),
            unit_price=Decimal("35000.00"),
            amount=Decimal("35000.00"),
            is_active=True,
        )
    )
    db_session.commit()

    preview = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)

    assert preview.actionable is False
    assert preview.existing_invoice_id == invoice.id


def test_legacy_payment_backed_repair_rejects_additional_coverage_overlap(
    db_session, subscriber, subscription
):
    query, _payment, _ledger, _entitlement = (
        _legacy_payment_backed_invoice_repair_fixture(
            db_session, subscriber, subscription
        )
    )
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=query.expected_period_start + timedelta(days=1),
            ends_at=query.expected_period_end,
            amount_funded=Decimal("1.00"),
            currency="NGN",
            status=ServiceEntitlementStatus.active,
            metadata_={"source": "synthetic_competing_coverage"},
        )
    )
    db_session.commit()

    preview = preview_legacy_payment_backed_renewal_invoice_repair(db_session, query)

    assert preview.actionable is False


def test_funding_event_without_payment_id_remains_retryable(db_session, subscriber):
    event = Event(
        event_type=EventType.payment_received,
        payload={},
        account_id=subscriber.id,
    )

    with pytest.raises(DomainError) as exc_info:
        PrepaidRenewalHandler().handle(db_session, event)

    assert exc_info.value.code.endswith("event_payment_missing")


def test_prepaid_monthly_charge_extracts_inclusive_catalog_vat(
    db_session, subscriber, subscription
):
    ensure_test_prepaid_contract(db_session, subscription, Decimal("10750.00"))
    price = db_session.scalar(
        select(OfferPrice).where(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
    )
    assert price is not None
    price.tax_application = TaxApplication.inclusive
    vat = TaxRate(
        name="Inclusive prepaid VAT",
        code="INCLUSIVE-PREPAID-VAT",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    db_session.add(vat)
    db_session.flush()
    subscriber.tax_rate_id = vat.id
    db_session.flush()

    detail = resolve_prepaid_monthly_charge_detail(
        db_session,
        subscription,
        datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert detail is not None
    assert detail.subtotal == Decimal("10000.00")
    assert detail.tax_total == Decimal("750.00")
    assert detail.total == Decimal("10750.00")
    assert detail.tax_application is TaxApplication.inclusive


def test_historical_debt_payment_event_does_not_run_current_renewal(db_session):
    event = Event(
        event_type=EventType.payment_received,
        payload={"access_consequence": "historical_debt_settlement_only"},
    )

    PrepaidRenewalHandler().handle(db_session, event)


def test_succeeded_payment_without_settlement_evidence_remains_retryable(
    db_session,
    subscriber,
):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime(2026, 7, 23, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(payment)
    db_session.flush()
    event = Event(
        event_type=EventType.payment_received,
        payload={"payment_id": str(payment.id)},
        account_id=subscriber.id,
    )

    with pytest.raises(PrepaidServiceRenewalError) as exc_info:
        PrepaidRenewalHandler().handle(db_session, event)

    assert exc_info.value.code.endswith("settlement_missing")


def _prepare(db_session, subscriber, subscription, amount="100.00"):
    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    ensure_test_prepaid_contract(db_session, subscription, Decimal("50.00"))
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal(amount),
        position_at=datetime(2026, 6, 30, tzinfo=UTC),
    )


def _preview(db_session, subscription, amount="50.00"):
    return preview_prepaid_service_renewal(
        db_session,
        subscription_id=subscription.id,
        starts_at=datetime(2026, 7, 1, tzinfo=UTC),
        ends_at=datetime(2026, 7, 31, tzinfo=UTC),
        amount=Decimal(amount),
        currency="NGN",
    )


def test_prepaid_service_renewal_creates_paid_invoice_and_entitlement(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)

    assert preview.funding_before == Decimal("100.00")
    assert preview.funding_after == Decimal("50.00")
    assert preview.allowed is True

    result = confirm_prepaid_service_renewal(
        db_session,
        preview,
        effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        evidence_ref="pytest:reviewed-service-cycle",
    )

    assert result.invoice is not None
    assert result.invoice_line is not None
    assert result.invoice.status is InvoiceStatus.paid
    assert result.invoice.total == Decimal("50.00")
    assert result.invoice.balance_due == Decimal("0.00")
    assert result.invoice.billing_period_start.replace(tzinfo=UTC) == datetime(
        2026, 7, 1, tzinfo=UTC
    )
    assert result.invoice.billing_period_end.replace(tzinfo=UTC) == datetime(
        2026, 7, 31, tzinfo=UTC
    )
    assert result.entitlement.source_invoice_id == result.invoice.id
    assert result.entitlement.source_invoice_line_id == result.invoice_line.id
    assert result.entitlement.subscription_id == subscription.id
    assert result.adjustment is None
    assert result.ledger_entry is None
    assert db_session.query(AccountAdjustment).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")
    db_session.refresh(subscription)
    assert subscription.next_billing_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 31, tzinfo=UTC
    )


def test_reviewed_missed_renewal_requires_exact_preview_fingerprint(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    subscription_id = subscription.id
    db_session.commit()

    with pytest.raises(PrepaidServiceRenewalError) as exc_info:
        execute_reviewed_prepaid_service_renewal(
            db_session,
            ExecuteReviewedPrepaidServiceRenewalCommand(
                context=CommandContext.system(
                    actor="operator:pytest",
                    scope="prepaid-renewal:test",
                    reason="pytest reviewed missed period",
                    idempotency_key="reviewed-missed-period",
                ),
                subscription_id=subscription_id,
                starts_at=preview.starts_at,
                ends_at=preview.ends_at,
                amount=preview.amount,
                currency=preview.currency,
                expected_preview_fingerprint="f" * 64,
                evidence_ref="finance-review:pytest",
            ),
        )

    assert exc_info.value.code.endswith("stale_preview")
    assert db_session.query(AccountAdjustment).count() == 0


def test_prepaid_service_renewal_is_idempotent(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    first = confirm_prepaid_service_renewal(
        db_session,
        preview,
        effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        evidence_ref="pytest:reviewed-service-cycle",
    )
    replay = confirm_prepaid_service_renewal(
        db_session,
        preview,
        effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        evidence_ref="pytest:reviewed-service-cycle",
    )

    assert replay.replayed is True
    assert replay.invoice is not None
    assert first.invoice is not None
    assert replay.invoice.id == first.invoice.id
    assert db_session.query(Invoice).count() == 1
    assert db_session.query(InvoiceLine).count() == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")


def test_prepaid_service_renewal_replay_checks_under_account_lock(
    db_session, subscriber, subscription, monkeypatch
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        evidence_ref="pytest:reviewed-service-cycle",
    )

    locks: list[str] = []
    monkeypatch.setattr(
        "app.services.prepaid_service_renewals.lock_account",
        lambda _db, account_id: locks.append(account_id),
    )

    replay = confirm_prepaid_service_renewal(
        db_session,
        preview,
        effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        evidence_ref="pytest:reviewed-service-cycle",
    )

    assert replay.replayed is True
    assert locks == [str(subscriber.id)]


def test_prepaid_service_renewal_rejects_insufficient_canonical_funding(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription, amount="40.00")
    preview = _preview(db_session, subscription)

    assert preview.allowed is False
    assert preview.shortfall == Decimal("10.00")
    with pytest.raises(PrepaidServiceRenewalError) as exc:
        confirm_prepaid_service_renewal(
            db_session,
            preview,
            effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            evidence_ref="pytest:reviewed-service-cycle",
        )
    assert exc.value.code.endswith("insufficient_funding")
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(Invoice).count() == 0


def test_prepaid_service_renewal_rejects_overlapping_entitlement(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        effective_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        evidence_ref="pytest:reviewed-service-cycle",
    )

    with pytest.raises(PrepaidServiceRenewalError, match="already has active funding"):
        preview_prepaid_service_renewal(
            db_session,
            subscription_id=subscription.id,
            starts_at=datetime(2026, 7, 15, tzinfo=UTC),
            ends_at=datetime(2026, 8, 15, tzinfo=UTC),
            amount=Decimal("50.00"),
            currency="NGN",
        )


def _prepare_scheduled_cycle(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription)
    subscription.offer.billing_cycle = BillingCycle.monthly
    subscription.offer.is_active = True
    db_session.commit()


def _add_due_account_without_baseline(db_session, subscriber, subscription):
    account = Subscriber(
        first_name="Missing",
        last_name="Baseline",
        email="missing-baseline@example.invalid",
        billing_mode=BillingMode.prepaid,
        reseller_id=subscriber.reseller_id,
        created_at=datetime(2026, 6, 29, tzinfo=UTC),
    )
    db_session.add(account)
    db_session.flush()
    due_subscription = Subscription(
        subscriber_id=account.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        unit_price=Decimal("50.00"),
    )
    db_session.add(due_subscription)
    db_session.commit()
    return account, due_subscription


def test_scheduled_owner_requires_an_active_owner_command(
    db_session, subscriber, subscription
):
    """`run_due_prepaid_service_renewals` is an internal function, never the
    production entry point on its own -- nightly per-account isolation
    (`execute_owner_savepoint`) requires an active owner command, which only
    exists via `execute_due_prepaid_service_renewals`. This is a deliberate,
    tested invariant of the owner-command boundary, not just something the
    3 fixed fixtures below happen to satisfy.
    """
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    with pytest.raises(
        RuntimeError, match="Owner savepoints require an active owner command"
    ):
        run_due_prepaid_service_renewals(
            db_session,
            run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        )


def test_scheduled_owner_funds_current_due_cycle(db_session, subscriber, subscription):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    # Routed through the real production entry point (`execute_due_prepaid_
    # service_renewals` -> `execute_owner_command`), not the bare function:
    # nightly per-account isolation depends on an active owner command
    # (`execute_owner_savepoint` requires one), which only exists here.
    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(
            datetime(2026, 7, 1, 12, tzinfo=UTC),
            key="pytest:scheduled-owner-funds-current-due-cycle",
        ),
    )
    db_session.commit()

    assert summary["prepaid_renewals_scanned"] == 1
    assert summary["prepaid_renewals_funded"] == 1
    invoice = db_session.query(Invoice).one()
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")


def _scheduled_command(run_at: datetime, *, key: str):
    return RunDuePrepaidServiceRenewalsCommand(
        context=CommandContext.system(
            actor="pytest:billing_automation",
            scope="prepaid_service_renewals",
            reason="verify the scheduled owner root",
            idempotency_key=key,
        ),
        run_at=run_at,
    )


def test_scheduled_public_owner_stages_paid_invoice_consumption_group(
    db_session, subscriber, subscription
):
    from app.models.customer_subledger import (
        CustomerPositionEffect,
        CustomerPostingGroup,
        PositionEffectKind,
        PostingCommandKind,
    )

    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    run_at = datetime(2026, 7, 1, 12, tzinfo=UTC)

    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(run_at, key="pytest:scheduled-renewal-owner"),
    )

    assert summary["prepaid_renewals_funded"] == 1
    group = db_session.query(CustomerPostingGroup).one()
    invoice = db_session.query(Invoice).one()
    entitlement = db_session.query(ServiceEntitlement).one()
    assert group.command_kind is PostingCommandKind.prepaid_consumption
    assert group.producer_owner == "financial.prepaid_draft_reconciliation"
    assert group.source_kind == "prepaid_opening_funding_consumption"
    assert group.source_id is not None
    effects = (
        db_session.query(CustomerPositionEffect)
        .filter(CustomerPositionEffect.group_id == group.id)
        .all()
    )
    assert {effect.effect for effect in effects} == {
        PositionEffectKind.prepaid_funding_consumed,
    }
    assert {effect.invoice_id for effect in effects} == {invoice.id}
    assert {effect.entitlement_id for effect in effects} == {None}
    assert {Decimal(str(effect.amount)) for effect in effects} == {Decimal("50.00")}

    # The same scheduled instant observes no second due period and never
    # duplicates the already committed group.
    db_session.commit()
    replay = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(run_at, key="pytest:scheduled-renewal-owner"),
    )
    assert replay["prepaid_renewals_funded"] == 0
    assert db_session.query(CustomerPostingGroup).count() == 1
    assert db_session.query(Invoice).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1


def test_scheduled_posting_failure_rolls_back_renewal_business_result(
    db_session, subscriber, subscription, monkeypatch
):
    from app.models.customer_subledger import CustomerPostingGroup

    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("renewal posting unavailable")

    monkeypatch.setattr(
        "app.services.billing.customer_subledger.stage_posting_group", _boom
    )
    with pytest.raises(RuntimeError, match="renewal posting unavailable"):
        execute_due_prepaid_service_renewals(
            db_session,
            _scheduled_command(
                datetime(2026, 7, 1, 12, tzinfo=UTC),
                key="pytest:scheduled-renewal-atomicity",
            ),
        )

    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(Invoice).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert db_session.query(CustomerPostingGroup).count() == 0


def test_funding_event_uses_owner_root_for_renewal_consumption(
    db_session, subscriber, subscription
):
    from app.models.customer_subledger import CustomerPostingGroup

    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    paid_at = datetime(2026, 7, 20, 17, 30, tzinfo=UTC)
    payment = create_test_settled_payment_credit(
        db_session,
        subscriber.id,
        Decimal("50.00"),
        paid_at=paid_at,
    )
    db_session.commit()

    from app.services import event_store as event_store_service

    event = Event(
        event_type=EventType.payment_received,
        payload={"payment_id": str(payment.id)},
        account_id=subscriber.id,
    )
    # A real `EventStore` row (2026-09 round 8): this test calls the
    # handler directly (not through the real dispatcher), but
    # `evaluate_prepaid_service_after_settlement`'s fail-closed guard now
    # requires `event_id` to resolve to a real, durable row -- this test's
    # payment carries genuine settlement evidence
    # (`create_test_settled_payment_credit`), so it reaches that guard
    # rather than failing earlier the way the two deliberately-incomplete
    # fixtures above do.
    event_store_service.create_event_record(db_session, event)
    db_session.flush()
    PrepaidRenewalHandler().handle(db_session, event)

    group = (
        db_session.query(CustomerPostingGroup)
        .filter_by(producer_owner="financial.account_credit_applications")
        .one()
    )
    assert group.command_kind.value == "customer_credit_application"
    assert group.source_kind == "payment_allocation"
    invoice = db_session.query(Invoice).one()
    allocation = db_session.query(PaymentAllocation).one()
    assert invoice.status is InvoiceStatus.paid
    assert allocation.invoice_id == invoice.id


def test_scheduled_owner_dry_run_writes_nothing(db_session, subscriber, subscription):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        dry_run=True,
    )

    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(Invoice).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_scheduled_owner_skips_quarantine_and_funds_verified_account(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, 1, tzinfo=UTC)
    _account, excluded_subscription = _add_due_account_without_baseline(
        db_session, subscriber, subscription
    )

    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(
            datetime(2026, 7, 1, 12, tzinfo=UTC),
            key="pytest:scheduled-owner-skips-quarantine",
        ),
    )
    db_session.commit()

    assert summary["prepaid_renewals_scanned"] == 2
    assert summary["prepaid_renewals_quarantined"] == 1
    assert summary["prepaid_renewals_missing_baseline"] == 0
    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(Invoice).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    db_session.refresh(excluded_subscription)
    assert excluded_subscription.next_billing_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 1, tzinfo=UTC
    )


def test_scheduled_owner_isolates_unexpected_missing_baseline(
    db_session, subscriber, subscription, monkeypatch
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, 1, tzinfo=UTC)
    _add_due_account_without_baseline(db_session, subscriber, subscription)
    monkeypatch.setattr(
        "app.services.prepaid_funding_reconstruction."
        "prepaid_funding_incomplete_source_account_ids",
        lambda _db, _account_ids: set(),
    )

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        dry_run=True,
    )

    assert summary["prepaid_renewals_scanned"] == 2
    assert summary["prepaid_renewals_quarantined"] == 0
    assert summary["prepaid_renewals_missing_baseline"] == 1
    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(Invoice).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0


def test_scheduled_owner_refuses_catalog_fallback_without_contract_price(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.unit_price = None
    db_session.commit()

    # Routed through the real production entry point (2026-09, round 9 --
    # a second occurrence of the B1 caller-sweep gap): `run_due_prepaid_
    # service_renewals`'s account-level isolation savepoint requires an
    # active owner command, which only exists via
    # `execute_due_prepaid_service_renewals`.
    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(
            datetime(2026, 7, 1, 12, tzinfo=UTC),
            key="pytest:scheduled-owner-refuses-catalog-fallback",
        ),
    )

    assert summary["prepaid_renewals_missing_price"] == 1
    assert summary["prepaid_renewals_funded"] == 0
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(Invoice).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_scheduled_owner_refuses_historical_catch_up(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.commit()

    # Routed through the real production entry point (2026-09, round 9 --
    # a second occurrence of the B1 caller-sweep gap): same reason as above.
    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(
            datetime(2026, 7, 5, tzinfo=UTC),
            key="pytest:scheduled-owner-refuses-historical-catch-up",
        ),
    )

    assert summary["prepaid_renewals_stale_anchor"] == 1
    assert summary["prepaid_renewals_funded"] == 0
    assert db_session.query(AccountAdjustment).count() == 0


def test_scheduled_owner_does_not_bill_suspended_prepaid_service(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    subscriber.status = SubscriberStatus.suspended
    subscriber.billing_enabled = True
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid-balance",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()
    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(
            datetime(2026, 7, 1, 12, tzinfo=UTC),
            key="pytest:scheduled-owner-restores-lock",
        ),
    )
    db_session.commit()

    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert summary["prepaid_renewals_scanned"] == 0
    assert summary["prepaid_renewals_funded"] == 0
    assert summary["prepaid_renewals_restored"] == 0
    assert lock.is_active is True
    assert subscription.status == SubscriptionStatus.suspended
    assert (
        db_session.query(EventStore)
        .filter_by(
            event_type="prepaid_service.renewed",
            subscription_id=subscription.id,
        )
        .count()
        == 0
    )


@pytest.mark.parametrize(
    ("effective_at", "expected_starts_at", "expected_ends_at"),
    (
        (
            datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
            datetime(2026, 7, 19, 23, tzinfo=UTC),
            datetime(2026, 8, 19, 23, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 20, 23, 30, tzinfo=UTC),
            datetime(2026, 7, 20, 23, tzinfo=UTC),
            datetime(2026, 8, 20, 23, tzinfo=UTC),
        ),
    ),
    ids=("within-utc-day", "lagos-next-day"),
)
def test_funding_change_renews_suspended_due_service_from_payment_day(
    db_session,
    subscriber,
    subscription,
    monkeypatch,
    effective_at,
    expected_starts_at,
    expected_ends_at,
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    subscriber.status = SubscriberStatus.suspended
    subscriber.billing_enabled = True
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid-funding-change",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.collections._core.resolve_prepaid_funding",
        lambda _db, account, *, now=None: PrepaidFundingDecision(
            account_id=str(account.id),
            available_balance=Decimal("50.00"),
            required_balance=Decimal("0.00"),
            currency="NGN",
            covered_subscription_ids=(subscription.id,),
        ),
    )
    monkeypatch.setattr(
        "app.services.account_lifecycle.emit_event",
        lambda *_args, **_kwargs: None,
    )

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=effective_at,
        funding_currency="NGN",
        evidence_ref="pytest:account-credit-event",
    )
    db_session.commit()

    db_session.refresh(lock)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    entitlement = db_session.query(ServiceEntitlement).one()
    assert result.disposition == FundingChangeRenewalDisposition.funded
    assert result.funded == 1
    assert result.restored_service_count == 0
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active
    assert subscriber.status == SubscriberStatus.active
    assert entitlement.starts_at.replace(tzinfo=UTC) == expected_starts_at
    assert entitlement.ends_at.replace(tzinfo=UTC) == expected_ends_at
    assert subscription.next_billing_at.replace(tzinfo=UTC) == expected_ends_at
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")
    assert len(result.renewals) == 1
    assert result.renewals[0].renewed_through == expected_ends_at
    assert result.renewals[0].source.value == "account_credit"
    event = (
        db_session.query(EventStore)
        .filter_by(event_type="prepaid_service.renewed")
        .one()
    )
    assert event.payload["renewed_through"] == expected_ends_at.isoformat()
    assert event.payload["schema_version"] == 2
    assert event.payload["invoice_id"] is not None
    assert str(event.invoice_id) == event.payload["invoice_id"]


def _stale_draft(db_session, subscriber, subscription, *, start, end):
    from app.models.billing import InvoiceLine

    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-STALE-{uuid4().hex[:8]}",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("50.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("50.00"),
        balance_due=Decimal("50.00"),
        billing_period_start=start,
        billing_period_end=end,
        is_proforma=False,
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("50.00"),
            amount=Decimal("50.00"),
            is_active=True,
        )
    )
    db_session.commit()
    return invoice


def _suspended_with_prepaid_lock(db_session, subscriber, subscription, monkeypatch):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    subscriber.status = SubscriberStatus.suspended
    subscriber.billing_enabled = True
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid-funding-change",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.collections._core.resolve_prepaid_funding",
        lambda _db, account, *, now=None: PrepaidFundingDecision(
            account_id=str(account.id),
            available_balance=Decimal("50.00"),
            required_balance=Decimal("0.00"),
            currency="NGN",
            covered_subscription_ids=(subscription.id,),
        ),
    )
    monkeypatch.setattr(
        "app.services.account_lifecycle.emit_event",
        lambda *_args, **_kwargs: None,
    )
    return lock


def test_funding_change_ignores_stale_draft_and_renews_current_period(
    db_session, subscriber, subscription, monkeypatch
):
    """SUB-005060 shape: a months-old draft must not absorb the new top-up."""
    from app.models.prepaid_funding import PrepaidDraftReconciliationException

    lock = _suspended_with_prepaid_lock(
        db_session, subscriber, subscription, monkeypatch
    )
    stale = _stale_draft(
        db_session,
        subscriber,
        subscription,
        start=datetime(2026, 6, 20, tzinfo=UTC),
        end=datetime(2026, 7, 20, tzinfo=UTC),
    )

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:stale-draft-new-topup",
    )
    db_session.commit()

    db_session.refresh(lock)
    db_session.refresh(subscription)
    db_session.refresh(stale)
    assert result.disposition == FundingChangeRenewalDisposition.funded
    assert result.draft_invoices_settled == 0
    assert stale.status is InvoiceStatus.draft
    assert stale.balance_due == Decimal("50.00")
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active
    exception = db_session.query(PrepaidDraftReconciliationException).one()
    assert exception.invoice_id == stale.id
    assert exception.reason == "stale_draft_period_ended_before_funding"


def test_funding_change_still_settles_current_period_draft(
    db_session, subscriber, subscription, monkeypatch
):
    from app.models.prepaid_funding import PrepaidDraftReconciliationException

    _suspended_with_prepaid_lock(db_session, subscriber, subscription, monkeypatch)
    current = _stale_draft(
        db_session,
        subscriber,
        subscription,
        start=datetime(2026, 7, 19, tzinfo=UTC),
        end=datetime(2026, 8, 19, tzinfo=UTC),
    )

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:current-draft-topup",
    )
    db_session.commit()

    db_session.refresh(current)
    assert result.draft_invoices_pending + result.draft_invoices_settled == 1
    assert (
        db_session.query(PrepaidDraftReconciliationException)
        .filter_by(reason="stale_draft_period_ended_before_funding")
        .count()
        == 0
    )


def test_funding_change_does_not_bill_administratively_suspended_service(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    subscriber.status = SubscriberStatus.suspended
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.admin,
        source="pytest:administrative-suspension",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:unrelated-account-credit-event",
    )

    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert result.disposition == FundingChangeRenewalDisposition.no_due_service
    assert result.scanned == 0
    assert result.funded == 0
    assert lock.is_active is True
    assert subscription.status == SubscriptionStatus.suspended
    assert db_session.query(ServiceEntitlement).count() == 0


def test_funding_change_leaves_service_due_while_payable_invoice_remains(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-FIRST-CLAIM",
            status=InvoiceStatus.partially_paid,
            currency="NGN",
            total=Decimal("75.00"),
            balance_due=Decimal("25.00"),
            is_active=True,
        )
    )
    db_session.commit()

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:account-credit-event",
    )

    assert result.disposition == (
        FundingChangeRenewalDisposition.payable_invoice_remaining
    )
    assert result.funded == 0
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
