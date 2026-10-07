"""Billing regression cases; use the normal fixture or migrated PostgreSQL lane."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentProvider,
    PaymentProviderType,
    ServiceEntitlement,
    TaxRate,
    TopupIntent,
)
from app.models.catalog import BillingMode
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.schemas.billing import PaymentProviderEventIngest
from app.services import prepaid_period_purchases as purchases
from app.services.billing._common import (
    get_reserved_purchase_credit_balance,
    get_spendable_account_credit_balance,
)
from app.services.billing.payments import PaymentAllocations
from app.services.owner_commands import CommandContext
from app.services.service_period_policy import PrepaidPeriodPurchasePolicy
from tests.payment_provider_event_helpers import stage_verified_provider_event
from tests.prepaid_funding_helpers import (
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)

payment_service = import_module("app.services.billing.payments")


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:period-purchase",
        scope="prepaid-period-purchase:settle",
        reason="Billing regression",
        idempotency_key=str(uuid4()),
    )


@pytest.fixture
def purchase_setup(db_session, active_subscription, monkeypatch, request):
    subscription = active_subscription
    monkeypatch.setattr(
        purchases, "_policy", lambda db: PrepaidPeriodPurchasePolicy(True, 12)
    )
    subscription.billing_mode = BillingMode.prepaid
    ensure_test_prepaid_contract(db_session, subscription, "100.00")
    if getattr(request, "param", False):
        rate = TaxRate(
            name="Purchase VAT",
            code="PURCHASE-VAT",
            rate=Decimal("7.5000"),
            is_active=True,
        )
        db_session.add(rate)
        db_session.flush()
        subscription.subscriber.tax_rate_id = rate.id
    materialize_test_prepaid_opening_balance(
        db_session, subscription.subscriber_id, "0.00"
    )
    now = datetime.now(UTC)
    quote = purchases.preview_prepaid_period_purchase(
        db_session,
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        period_count=2,
        effective_at=now,
    )
    command = purchases.CreatePrepaidPeriodPurchaseCommand(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        period_count=2,
        expected_fingerprint=quote.fingerprint,
        idempotency_key=str(uuid4()),
        created_by="pytest",
        effective_at=now,
    )
    db_session.commit()
    purchase = purchases.create_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    provider = PaymentProvider(
        name=f"Period Purchase {uuid4()}",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.flush()
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
        currency=purchase.currency,
        requested_amount=purchase.total,
        expires_at=purchase.expires_at,
        status="pending",
    )
    db_session.add(intent)
    db_session.flush()
    purchase.topup_intent_id = intent.id
    purchase.status = PrepaidPeriodPurchaseStatus.payment_pending
    settle = purchases.SettleVerifiedPrepaidPeriodPurchaseCommand(
        intent_id=intent.id,
        provider_id=provider.id,
        external_transaction_id=str(uuid4()),
        amount=purchase.total,
        provider_fee=Decimal("2.00"),
        currency=purchase.currency,
        effective_at=datetime.now(UTC),
    )
    purchase_id = purchase.id
    db_session.commit()
    return purchase_id, command, settle


def test_collected_money_survives_optional_settlement_rollback(
    db_session, purchase_setup, monkeypatch
):
    purchase_id, _, command = purchase_setup

    def reject(db, settlement):
        purchase = db.get(PrepaidPeriodPurchase, settlement.purchase_id)
        db.add(
            Invoice(
                account_id=purchase.account_id,
                status=InvoiceStatus.draft,
                currency="NGN",
                total=Decimal("10.00"),
            )
        )
        db.flush()
        raise purchases.PrepaidPeriodPurchaseError(
            code="financial.prepaid_period_purchases.stale_quote",
            message="Coverage moved",
        )

    monkeypatch.setattr(purchases, "settle_prepaid_period_purchase", reject)
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert result.status is PrepaidPeriodPurchaseStatus.review_required
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    payment = db_session.get(Payment, result.payment_id)
    assert payment.reserved_for_purchase_id == purchase_id
    assert purchase.payment_id == payment.id
    assert db_session.scalar(select(func.count(Invoice.id))) == 0
    assert (
        get_reserved_purchase_credit_balance(db_session, purchase.account_id)
        == command.amount
    )
    assert get_spendable_account_credit_balance(
        db_session, str(purchase.account_id)
    ) == Decimal("0.00")
    assert PaymentAllocations.available_amount(db_session, str(payment.id)) == Decimal(
        "0.00"
    )
    db_session.rollback()
    replay = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert replay.payment_id == payment.id
    assert db_session.scalar(select(func.count(Payment.id))) == 1


def test_intent_completion_failure_holds_cash_and_rolls_back_all_periods(
    db_session, purchase_setup, monkeypatch
):
    purchase_id, _, command = purchase_setup

    def reject_completion(*args, **kwargs):
        raise purchases.PrepaidPeriodPurchaseError(
            code="financial.prepaid_period_purchases.intent_invalid",
            message="Completion evidence changed",
        )

    monkeypatch.setattr(purchases, "stage_topup_intent_completion", reject_completion)
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert result.status is PrepaidPeriodPurchaseStatus.review_required
    assert (
        db_session.get(Payment, result.payment_id).reserved_for_purchase_id
        == purchase_id
    )
    assert (
        db_session.get(PrepaidPeriodPurchase, purchase_id).payment_id
        == result.payment_id
    )
    assert db_session.scalar(select(func.count(Invoice.id))) == 0
    assert db_session.scalar(select(func.count(ServiceEntitlement.id))) == 0
    assert db_session.get(TopupIntent, command.intent_id).status == "pending"


@pytest.mark.parametrize(
    ("participant", "method", "failure_code"),
    [
        (
            purchases.Invoices,
            "stage_system_invoice",
            "financial.prepaid_period_purchases.settlement_rejected",
        ),
        (
            purchases.InvoiceLines,
            "stage_system_line",
            "financial.prepaid_period_purchases.settlement_rejected",
        ),
        (
            purchases.Invoices,
            "issue_draft_system",
            "financial.prepaid_period_purchases.settlement_rejected",
        ),
        (
            payment_service,
            "_finalize_invoice_application",
            "financial.payments.invoice_application_rejected",
        ),
    ],
    ids=["invoice", "line", "issuance", "payment-finalization"],
)
def test_legacy_participant_rejection_preserves_cash_and_allows_exact_retry(
    db_session, purchase_setup, monkeypatch, participant, method, failure_code
):
    purchase_id, _, command = purchase_setup
    original = getattr(participant, method)
    calls = 0

    def reject_second_period(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=409, detail="Legacy billing validation")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(participant, method, reject_second_period)
        held = purchases.settle_verified_prepaid_period_purchase(
            db_session, command, context=_context()
        )
    assert calls == 2
    assert held.status is PrepaidPeriodPurchaseStatus.review_required
    assert held.failure_code == failure_code
    # The first period was fully staged before rejection of the second. Verify
    # the committed result from a new transaction, not the identity map alone.
    db_session.rollback()
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    assert purchase.payment_id == held.payment_id
    assert purchase.failure_code == failure_code
    assert db_session.scalar(select(func.count(Payment.id))) == 1
    assert db_session.scalar(select(func.count(Invoice.id))) == 0
    assert db_session.scalar(select(func.count(ServiceEntitlement.id))) == 0
    assert db_session.get(TopupIntent, command.intent_id).status == "pending"
    assert (
        get_reserved_purchase_credit_balance(db_session, purchase.account_id)
        == command.amount
    )
    assert get_spendable_account_credit_balance(
        db_session, str(purchase.account_id)
    ) == Decimal("0.00")
    db_session.rollback()
    completed = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert completed.status is PrepaidPeriodPurchaseStatus.completed
    assert completed.payment_id == held.payment_id
    assert len(completed.invoice_ids) == len(completed.entitlement_ids) == 2
    assert db_session.scalar(select(func.count(Payment.id))) == 1


def test_purchase_settles_exactly_once_and_rounds_each_invoice(
    db_session, purchase_setup
):
    _, _, command = purchase_setup
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert result.status is PrepaidPeriodPurchaseStatus.completed
    assert len(result.invoice_ids) == len(result.entitlement_ids) == 2
    invoices = list(
        db_session.scalars(
            select(Invoice).where(Invoice.id.in_(result.invoice_ids))
        ).all()
    )
    assert all(
        invoice.status is InvoiceStatus.paid and invoice.balance_due == 0
        for invoice in invoices
    )
    assert (
        sum((invoice.total for invoice in invoices), Decimal("0.00")) == command.amount
    )
    assert db_session.scalar(select(func.count(ServiceEntitlement.id))) == 2
    db_session.rollback()
    replay = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert replay.replayed and replay.invoice_ids == result.invoice_ids


def test_second_checkout_cannot_sell_the_same_dates(db_session, purchase_setup):
    _, original, _ = purchase_setup
    from dataclasses import replace

    with pytest.raises(
        purchases.PrepaidPeriodPurchaseError, match="awaiting payment or review"
    ):
        purchases.create_prepaid_period_purchase(
            db_session,
            replace(original, idempotency_key=str(uuid4())),
            context=_context(),
        )
    assert db_session.scalar(select(func.count(PrepaidPeriodPurchase.id))) == 1


@pytest.mark.parametrize("changed_evidence", ["amount", "currency"])
def test_wrong_collected_amount_or_currency_is_held_for_review(
    db_session, purchase_setup, changed_evidence
):
    _, _, original = purchase_setup
    from dataclasses import replace

    command = (
        replace(original, amount=original.amount - Decimal("1.00"))
        if changed_evidence == "amount"
        else replace(original, currency="USD")
    )
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert result.status is PrepaidPeriodPurchaseStatus.review_required
    assert db_session.get(Payment, result.payment_id).amount == command.amount
    assert db_session.get(Payment, result.payment_id).currency == command.currency
    assert db_session.scalar(select(func.count(Invoice.id))) == 0


@pytest.mark.parametrize("timely_capture", [False, True])
def test_delayed_confirmation_uses_provider_capture_time(
    db_session, purchase_setup, timely_capture
):
    _, _, original = purchase_setup
    from dataclasses import replace

    command = replace(
        original,
        effective_at=original.effective_at + timedelta(days=2),
        provider_paid_at=original.effective_at if timely_capture else None,
    )
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    expected = (
        PrepaidPeriodPurchaseStatus.completed
        if timely_capture
        else PrepaidPeriodPurchaseStatus.review_required
    )
    assert result.status is expected
    assert db_session.get(Payment, result.payment_id).amount == original.amount


def test_quote_timestamp_is_the_persisted_review_time(db_session, purchase_setup):
    purchase_id, quote_command, _ = purchase_setup
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    assert purchases._utc(purchase.created_at) == quote_command.effective_at
    current = purchases.preview_prepaid_period_purchase(
        db_session,
        account_id=purchase.account_id,
        subscription_id=purchase.subscription_id,
        period_count=purchase.period_count,
        effective_at=purchase.created_at.astimezone(UTC),
    )
    assert current.fingerprint == purchase.preview_fingerprint


def test_second_real_capture_is_preserved_without_replacing_primary(
    db_session, purchase_setup
):
    purchase_id, _, original = purchase_setup
    first = purchases.settle_verified_prepaid_period_purchase(
        db_session, original, context=_context()
    )
    assert first.status is PrepaidPeriodPurchaseStatus.completed
    db_session.rollback()
    second = purchases.settle_verified_prepaid_period_purchase(
        db_session,
        replace(original, external_transaction_id=str(uuid4())),
        context=_context(),
    )
    assert second.status is PrepaidPeriodPurchaseStatus.review_required
    assert second.payment_id != first.payment_id
    assert (
        db_session.get(PrepaidPeriodPurchase, purchase_id).payment_id
        == first.payment_id
    )
    assert (
        get_reserved_purchase_credit_balance(
            db_session,
            original_account := db_session.get(Payment, first.payment_id).account_id,
        )
        == original.amount
    )
    assert get_spendable_account_credit_balance(
        db_session, str(original_account)
    ) == Decimal("0.00")
    assert db_session.scalar(select(func.count(Invoice.id))) == 2
    assert (
        purchases.preview_purchase_recovery(db_session, purchase_id).action
        is purchases.PurchaseRecoveryAction.refund_or_provider_review
    )


def test_held_receipt_recovery_is_preview_bound_and_replayable(
    db_session, purchase_setup, monkeypatch
):
    purchase_id, _, command = purchase_setup
    settlement = purchases.settle_prepaid_period_purchase

    def reject(db, purchase_command):
        raise purchases.PrepaidPeriodPurchaseError(
            code="financial.prepaid_period_purchases.settlement_rejected",
            message="Temporary participant failure",
        )

    monkeypatch.setattr(purchases, "settle_prepaid_period_purchase", reject)
    held = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    monkeypatch.setattr(purchases, "settle_prepaid_period_purchase", settlement)
    preview = purchases.preview_purchase_recovery(db_session, purchase_id)
    assert preview.action is purchases.PurchaseRecoveryAction.retry_settlement
    from tests.period_purchase_review_helpers import create_review_staff

    principal = create_review_staff(db_session)
    db_session.commit()
    retry = purchases.RetryPurchaseSettlementCommand(
        purchase_id=purchase_id,
        expected_fingerprint=preview.fingerprint,
        effective_at=datetime.now(UTC),
        permission_granted=True,
        actor_system_user_id=principal.id,
    )
    context = CommandContext.system(
        actor=f"staff:{principal.id}",
        scope=purchases.PURCHASE_REPAIR_SCOPE,
        reason="Retry held settlement after correcting participant",
        idempotency_key=str(uuid4()),
    )
    db_session.rollback()
    recovered = purchases.retry_purchase_settlement(db_session, retry, context=context)
    assert recovered.status is PrepaidPeriodPurchaseStatus.completed
    assert recovered.payment_id == held.payment_id
    db_session.rollback()
    replay = purchases.retry_purchase_settlement(db_session, retry, context=context)
    assert replay.replayed and replay.invoice_ids == recovered.invoice_ids


def test_recovery_cannot_be_authorized_by_an_actor_label(db_session, purchase_setup):
    purchase_id, _, _ = purchase_setup
    preview = purchases.preview_purchase_recovery(db_session, purchase_id)
    command = purchases.RetryPurchaseSettlementCommand(
        purchase_id=purchase_id,
        expected_fingerprint=preview.fingerprint,
        effective_at=datetime.now(UTC),
        permission_granted=False,
        actor_system_user_id=uuid4(),
    )
    db_session.rollback()
    with pytest.raises(purchases.PrepaidPeriodPurchaseError, match="permission"):
        purchases.retry_purchase_settlement(db_session, command, context=_context())


def test_confirmed_refund_releases_held_purchase_without_spendable_surplus(
    db_session, purchase_setup, monkeypatch
):
    purchase_id, _, command = purchase_setup

    def reject(db, purchase_command):
        raise purchases.PrepaidPeriodPurchaseError(
            code="financial.prepaid_period_purchases.stale_quote", message="Review"
        )

    monkeypatch.setattr(purchases, "settle_prepaid_period_purchase", reject)
    held = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    stage_verified_provider_event(
        db_session,
        PaymentProviderEventIngest(
            provider_id=command.provider_id,
            payment_id=held.payment_id,
            event_type="refund.processed",
            amount=command.amount,
            currency=command.currency,
            idempotency_key=str(uuid4()),
        ),
    )
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    assert purchase.status is PrepaidPeriodPurchaseStatus.canceled
    assert get_reserved_purchase_credit_balance(
        db_session, purchase.account_id
    ) == Decimal("0.00")
    assert get_spendable_account_credit_balance(
        db_session, str(purchase.account_id)
    ) == Decimal("0.00")


def test_refunding_an_additional_capture_preserves_original_purchased_periods(
    db_session, purchase_setup
):
    purchase_id, _, command = purchase_setup
    first = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    db_session.rollback()
    additional = purchases.settle_verified_prepaid_period_purchase(
        db_session,
        replace(command, external_transaction_id=str(uuid4())),
        context=_context(),
    )
    preview = purchases.preview_purchase_recovery(db_session, purchase_id)
    assert {row.payment_id for row in preview.receipts} == {
        first.payment_id,
        additional.payment_id,
    }
    stage_verified_provider_event(
        db_session,
        PaymentProviderEventIngest(
            provider_id=command.provider_id,
            payment_id=additional.payment_id,
            event_type="refund.processed",
            amount=command.amount,
            currency=command.currency,
            idempotency_key=str(uuid4()),
        ),
    )
    assert (
        db_session.get(PrepaidPeriodPurchase, purchase_id).status
        is PrepaidPeriodPurchaseStatus.completed
    )
    assert all(
        db_session.get(Invoice, invoice_id).status is InvoiceStatus.paid
        for invoice_id in first.invoice_ids
    )


@pytest.mark.parametrize("purchase_setup", [True], indirect=True)
def test_purchase_invoice_lines_use_the_reviewed_tax_facts(db_session, purchase_setup):
    from app.models.billing import InvoiceLine

    _, _, command = purchase_setup
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert result.status is PrepaidPeriodPurchaseStatus.completed
    lines = list(
        db_session.scalars(
            select(InvoiceLine).where(InvoiceLine.invoice_id.in_(result.invoice_ids))
        ).all()
    )
    assert all(
        line.tax_rate_code_snapshot == "PURCHASE-VAT"
        and line.tax_rate_percent_snapshot == Decimal("7.5000")
        for line in lines
    )
    assert (
        sum(
            (
                db_session.get(Invoice, invoice_id).total
                for invoice_id in result.invoice_ids
            ),
            Decimal("0.00"),
        )
        == command.amount
    )


@pytest.mark.parametrize("purchase_setup", [True], indirect=True)
def test_same_amount_tax_provenance_change_still_requires_review(
    db_session, purchase_setup
):
    purchase_id, _, command = purchase_setup
    rate = db_session.scalar(select(TaxRate).where(TaxRate.code == "PURCHASE-VAT"))
    rate.code = "CHANGED-VAT-CODE"
    db_session.commit()
    result = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    assert result.status is PrepaidPeriodPurchaseStatus.review_required
    assert result.failure_code == "financial.prepaid_period_purchases.stale_quote"
    assert (
        db_session.get(PrepaidPeriodPurchase, purchase_id).payment_id
        == result.payment_id
    )
    assert db_session.scalar(select(func.count(Invoice.id))) == 0


def test_historical_additional_capture_survives_a_new_live_checkout(
    db_session, purchase_setup
):
    purchase_id, _, command = purchase_setup
    first = purchases.settle_verified_prepaid_period_purchase(
        db_session, command, context=_context()
    )
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    quote = purchases.preview_prepaid_period_purchase(
        db_session,
        account_id=purchase.account_id,
        subscription_id=purchase.subscription_id,
        period_count=2,
        effective_at=datetime.now(UTC),
    )
    next_command = purchases.CreatePrepaidPeriodPurchaseCommand(
        account_id=purchase.account_id,
        subscription_id=purchase.subscription_id,
        period_count=2,
        expected_fingerprint=quote.fingerprint,
        idempotency_key=str(uuid4()),
        created_by="pytest",
        effective_at=datetime.now(UTC),
    )
    db_session.rollback()
    next_purchase = purchases.create_prepaid_period_purchase(
        db_session, next_command, context=_context()
    )
    next_id = next_purchase.id
    db_session.rollback()
    additional = purchases.settle_verified_prepaid_period_purchase(
        db_session,
        replace(command, external_transaction_id=str(uuid4())),
        context=_context(),
    )
    assert additional.status is PrepaidPeriodPurchaseStatus.review_required
    assert (
        db_session.get(PrepaidPeriodPurchase, next_id).status
        is PrepaidPeriodPurchaseStatus.quoted
    )
    assert (
        db_session.get(PrepaidPeriodPurchase, purchase_id).payment_id
        == first.payment_id
    )
    assert db_session.scalar(select(func.count(Payment.id))) == 2
