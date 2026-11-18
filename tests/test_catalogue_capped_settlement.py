"""Capped drafts promise a package; only verified payment determines validity."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
)
from app.models.catalog import BillingMode, UsageAllowance, UsageAllowanceResetBasis
from app.models.usage import QuotaBucket
from app.services.domain_errors import DomainError
from app.services.service_entitlements import (
    ensure_prepaid_entitlements_for_paid_invoice,
)


def _pending(db, subscriber, subscription, *, funded=True):
    subscription.billing_mode = BillingMode.prepaid
    allowance = UsageAllowance(
        name="Capped 100 GB",
        included_gb=100,
        reset_basis=UsageAllowanceResetBasis.renewal_cycle,
        validity_days=30,
        is_active=True,
    )
    db.add(allowance)
    db.flush()
    subscription.offer.usage_allowance_id = allowance.id
    subscription.offer.usage_allowance = allowance
    paid_at = datetime(2026, 11, 8, 13, 17, tzinfo=UTC)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("100"),
        total=Decimal("100"),
        balance_due=Decimal("0"),
        issued_at=paid_at,
        paid_at=paid_at,
    )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="100 GB renewal",
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
        amount=Decimal("100"),
        metadata_={
            "kind": "base_subscription",
            "future_catalogue_invoice": True,
            "future_invoice": {
                "period_kind": "capped_pending_renewal",
                "allowance_gb": "100",
                "validity_days": 30,
            },
        },
    )
    db.add(line)
    if funded:
        payment = Payment(
            account_id=subscriber.id,
            amount=Decimal("100"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=paid_at,
            is_active=True,
        )
        db.add(payment)
        db.flush()
        db.add(
            PaymentSettlement(
                payment_id=payment.id,
                amount=Decimal("100"),
                currency="NGN",
                origin=PaymentSettlementOrigin.manual,
                unallocated_amount=Decimal("0"),
            )
        )
        db.add(
            PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=Decimal("100"),
                is_active=True,
                created_at=paid_at,
            )
        )
    db.flush()
    return invoice, line, paid_at, allowance


def test_capped_full_payment_uses_exact_payment_instant_and_initializes_quota(
    db_session, subscriber, subscription
):
    invoice, line, paid_at, _ = _pending(db_session, subscriber, subscription)
    # Earlier valid coverage intentionally overlaps the new quota renewal.
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=subscription.id,
            starts_at=paid_at - timedelta(days=1),
            ends_at=paid_at + timedelta(days=12),
            amount_funded=Decimal("100"),
            currency="NGN",
        )
    )
    db_session.flush()
    created = ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    assert len(created) == 1
    assert created[0].starts_at == paid_at
    assert created[0].ends_at == paid_at + timedelta(days=30)
    assert invoice.billing_period_start is None  # original invoice promised no dates
    assert line.metadata_["billing_period_source"] == "capped_catalogue_settlement"
    bucket = db_session.scalar(
        select(QuotaBucket).where(QuotaBucket.subscription_id == subscription.id)
    )
    assert bucket is not None
    assert bucket.period_start.replace(tzinfo=UTC) == paid_at
    assert ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice) == []
    assert (
        db_session.query(QuotaBucket).filter_by(subscription_id=subscription.id).count()
        == 1
    )


def test_paid_label_without_verified_funding_does_not_activate(
    db_session, subscriber, subscription
):
    invoice, _, _, _ = _pending(db_session, subscriber, subscription, funded=False)
    with pytest.raises(DomainError, match="complete verified settlement"):
        ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    assert (
        db_session.query(ServiceEntitlement)
        .filter_by(source_invoice_id=invoice.id)
        .count()
        == 0
    )


def test_capped_validity_change_requires_review_before_activation(
    db_session, subscriber, subscription
):
    invoice, _, _, allowance = _pending(db_session, subscriber, subscription)
    allowance.validity_days = 60
    db_session.flush()
    with pytest.raises(DomainError, match="policy changed"):
        ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)


def test_partial_payment_cannot_start_capped_validity(
    db_session, subscriber, subscription
):
    invoice, _, _, _ = _pending(db_session, subscriber, subscription)
    invoice.status = InvoiceStatus.partially_paid
    invoice.balance_due = Decimal("10")
    assert ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice) == []
    assert (
        db_session.query(QuotaBucket).filter_by(subscription_id=subscription.id).count()
        == 0
    )


def test_native_cash_and_withholding_use_native_settlement_effect_instant(
    db_session, subscriber, subscription, monkeypatch
):
    from uuid import uuid4

    from dotmac_billing import queries
    from dotmac_billing.contracts import CurrentInvoiceReceivableV1
    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.money import Currency, Money

    from app.models.billing import StarterInvoiceLink
    from app.services.operator_tenant import OPERATOR_TENANT_ID

    invoice, line, paid_at, _ = _pending(
        db_session, subscriber, subscription, funded=False
    )
    accepted_at = paid_at + timedelta(days=2)
    link = StarterInvoiceLink(
        invoice_id=invoice.id,
        tenant_id=OPERATOR_TENANT_ID,
        billing_account_id=uuid4(),
        document_id=uuid4(),
        source_fact_id=f"native:{invoice.id}",
    )
    db_session.add(link)
    db_session.flush()
    scope = TenantScope(OPERATOR_TENANT_ID)
    currency = Currency("NGN", 2)
    allocation_id, withholding_id = uuid4(), uuid4()
    fact = CurrentInvoiceReceivableV1(
        scope,
        link.billing_account_id,
        link.document_id,
        3,
        1,
        Money(Decimal("100"), currency),
        Money(Decimal("0"), currency),
        Money(Decimal("100"), currency),
        Money(Decimal("0"), currency),
        Money(Decimal("95"), currency),
        Money(Decimal("5"), currency),
        True,
        accepted_at,
        (allocation_id,),
        (withholding_id,),
    )
    monkeypatch.setattr(
        queries, "get_current_invoice_receivable_v1", lambda db, **kwargs: fact
    )
    created = ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
    assert created[0].starts_at == accepted_at
    assert created[0].ends_at == accepted_at + timedelta(days=30)
    assert line.metadata_["capped_activation_document_id"] == str(link.document_id)
    assert line.metadata_["capped_activation_withholding_ids"] == [str(withholding_id)]
    assert (
        db_session.scalar(select(Payment.id).where(Payment.account_id == subscriber.id))
        is None
    )


def test_native_unrelated_credit_does_not_substitute_for_capped_cash_funding(
    db_session, subscriber, subscription, monkeypatch
):
    from uuid import uuid4

    from dotmac_billing import queries
    from dotmac_billing.contracts import CurrentInvoiceReceivableV1
    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.money import Currency, Money

    from app.models.billing import StarterInvoiceLink
    from app.services.operator_tenant import OPERATOR_TENANT_ID

    invoice, _, paid_at, _ = _pending(
        db_session, subscriber, subscription, funded=False
    )
    link = StarterInvoiceLink(
        invoice_id=invoice.id,
        tenant_id=OPERATOR_TENANT_ID,
        billing_account_id=uuid4(),
        document_id=uuid4(),
        source_fact_id=f"native:{invoice.id}",
    )
    db_session.add(link)
    db_session.flush()
    currency = Currency("NGN", 2)
    fact = CurrentInvoiceReceivableV1(
        TenantScope(OPERATOR_TENANT_ID),
        link.billing_account_id,
        link.document_id,
        3,
        1,
        Money(Decimal("100"), currency),
        Money(Decimal("0"), currency),
        Money(Decimal("100"), currency),
        Money(Decimal("0"), currency),
        Money(Decimal("50"), currency),
        Money(Decimal("0"), currency),
        True,
        paid_at,
    )
    monkeypatch.setattr(
        queries, "get_current_invoice_receivable_v1", lambda db, **kwargs: fact
    )
    with pytest.raises(DomainError, match="complete verified settlement"):
        ensure_prepaid_entitlements_for_paid_invoice(db_session, invoice)
