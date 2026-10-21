"""Gateway period purchases fund through Starter (migrated PostgreSQL).

Checkout side: an eligible prepaid period-purchase checkout opens the
``dotmac-payments`` PURCHASE_FUNDING intent bound to the purchase
identity, in the same transaction as the legacy ``TopupIntent``; with
the Starter live switch OFF nothing touches the payments module.

Settlement side: the verified provider confirmation for that bound
intent funds the purchase through ``starter_prepaid`` — earmarks per
period, Starter-issued period invoices settling to a zero collectible
mirror, entitlements and anchors projected exactly as legacy, and the
legacy provider Payment left unallocated as evidence only.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_kernel.cache import TenantScope
from dotmac_payments import GetPaymentIntentV1, PaymentPurpose, get_payment_intent_v1
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    ServiceEntitlement,
    StarterInvoiceLink,
    StarterPrepaidFundingLink,
    TopupIntent,
)
from app.models.catalog import BillingMode
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.services import gateway_topup_intents as svc
from app.services import prepaid_period_purchases as purchases
from app.services.billing import starter_policies, starter_prepaid
from app.services.brand_profiles import upsert_brand_profile
from app.services.db_session_adapter import db_session_adapter
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.test_gateway_topup_intents import _checkout_binding, _context, _patch_policy
from tests.test_prepaid_period_purchase_safety import _context as purchase_context
from tests.test_prepaid_period_purchase_safety import (
    purchase_setup as _purchase_fixture,
)

pytestmark = pytest.mark.integration

_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_SERIES = f"invgw-{uuid4().hex[:8]}"


@pytest.fixture(name="purchase_setup")
def _gateway_purchase_setup(db_session, active_subscription, monkeypatch, request):
    return _purchase_fixture.__wrapped__(
        db_session, active_subscription, monkeypatch, request
    )


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "PRE"
        if key == "starter_ledger_live_enabled":
            return True
        return real_resolve(db, domain, key)

    monkeypatch.setattr(starter_policies, "resolve_value", _resolve)


@pytest.fixture
def _seller_and_bank(db_session):
    from app.models.billing import CollectionAccount, CollectionAccountType

    upsert_brand_profile(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={
            "legal_name": "Dotmac Technologies Ltd",
            "legal_address": {
                "street1": "Plot 1 Proof Way",
                "city": "Abuja",
                "country": "NGA",
            },
        },
    )
    db_session.add(
        CollectionAccount(
            name=f"Gateway Collection {uuid4().hex[:8]}",
            account_type=CollectionAccountType.bank,
            bank_name="Dotmac Bank",
            account_name="Dotmac Technologies Ltd",
            account_number="0123456789",
            account_last4="6789",
            sort_code="12-34-56",
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()


def _checkout_purchase(db_session, monkeypatch, purchase, *, reference):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)
    return svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.prepaid_period_purchase,
            account_id=purchase.account_id,
            purchase_id=purchase.id,
            reference=reference,
            provider_type="paystack",
            provider_id=None,
            created_by="pytest",
            expected_preview_fingerprint=purchase.preview_fingerprint,
            capability_binding_id=binding.id,
        ),
        context=_context(svc.CREATE_CUSTOMER_SCOPE, idempotency_key=f"gw-{uuid4()}"),
    )


def _bare_purchase(db_session, subscriber, subscription):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    purchase = PrepaidPeriodPurchase(
        account_id=subscriber.id,
        subscription_id=subscription.id,
        status=PrepaidPeriodPurchaseStatus.quoted,
        period_count=2,
        currency="NGN",
        coverage_starts_at=now,
        coverage_ends_at=now + timedelta(days=60),
        subtotal=Decimal("15000.00"),
        tax_total=Decimal("1125.00"),
        total=Decimal("16125.00"),
        preview_fingerprint="b" * 64,
        policy_version=1,
        policy_snapshot={},
        idempotency_key=f"gw-purchase-{uuid4()}",
        created_by="pytest",
        expires_at=now + timedelta(minutes=30),
    )
    db_session.add(purchase)
    db_session.commit()
    return purchase


def test_eligible_checkout_opens_bound_funding_intent(
    monkeypatch, db_session, subscriber, subscription, _starter_series_isolation
):
    subscriber.billing_mode = BillingMode.prepaid
    db_session.commit()
    purchase = _bare_purchase(db_session, subscriber, subscription)
    purchase_id = purchase.id
    reference = f"gw-funding-{uuid4()}"

    db_session_adapter.release_read_transaction(db_session)
    _checkout_purchase(db_session, monkeypatch, purchase, reference=reference)

    observed = get_payment_intent_v1(
        db_session, scope=_SCOPE, query=GetPaymentIntentV1(reference=reference)
    )
    assert observed is not None
    assert observed.purpose is PaymentPurpose.PURCHASE_FUNDING
    assert observed.target_reference == str(purchase_id)
    assert observed.payer_reference == str(subscriber.id)
    assert observed.requested.amount == Decimal("16125.00")


def test_checkout_without_live_switch_opens_no_module_intent(
    monkeypatch, db_session, subscriber, subscription
):
    subscriber.billing_mode = BillingMode.prepaid
    db_session.commit()
    purchase = _bare_purchase(db_session, subscriber, subscription)
    reference = f"gw-legacy-{uuid4()}"

    db_session_adapter.release_read_transaction(db_session)
    _checkout_purchase(db_session, monkeypatch, purchase, reference=reference)

    observed = get_payment_intent_v1(
        db_session, scope=_SCOPE, query=GetPaymentIntentV1(reference=reference)
    )
    assert observed is None


def test_verified_purchase_settles_through_starter_funding(
    db_session,
    purchase_setup,
    _starter_series_isolation,
    _seller_and_bank,
):
    purchase_id, _command, settle = purchase_setup
    purchase = db_session.get(PrepaidPeriodPurchase, purchase_id)
    assert purchase is not None
    from app.models.subscriber import Subscriber

    subscriber = db_session.get(Subscriber, purchase.account_id)
    assert subscriber is not None
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    intent = db_session.get(TopupIntent, purchase.topup_intent_id)
    assert intent is not None
    db_session.commit()

    from dotmac_kernel.money import Currency, Money
    from dotmac_payments import OpenPaymentIntent, open_payment_intent

    open_payment_intent(
        db_session,
        scope=_SCOPE,
        command=OpenPaymentIntent(
            reference=intent.reference,
            payer_reference=str(purchase.account_id),
            purpose=PaymentPurpose.PURCHASE_FUNDING,
            target_reference=str(purchase.id),
            requested=Money(purchase.total, Currency("NGN", 2)),
            provider_type="paystack",
            channel="customer_selfcare",
            opened_at=intent.created_at,
            expires_at=intent.expires_at,
        ),
    )
    db_session.commit()

    db_session_adapter.release_read_transaction(db_session)
    # TEMP-PROBE: name the participant whose soft rollback poisons the
    # owner boundary; remove once the composition gap is fixed.
    import traceback

    from sqlalchemy import event
    from sqlalchemy.orm import Session as _Session

    _stacks: list[str] = []

    def _probe(session, previous_transaction):
        _stacks.append("".join(traceback.format_stack(limit=18)))

    event.listen(_Session, "after_soft_rollback", _probe)
    try:
        result = purchases.settle_verified_prepaid_period_purchase(
            db_session, settle, context=purchase_context()
        )
    except Exception as exc:
        raise AssertionError(
            f"{type(exc).__name__}: {exc}\nSOFT-ROLLBACK STACKS:\n"
            + "\n---\n".join(_stacks[-4:])
        ) from exc
    finally:
        event.remove(_Session, "after_soft_rollback", _probe)
    db_session.commit()

    assert result.status is PrepaidPeriodPurchaseStatus.completed
    assert len(result.invoice_ids) == 2
    assert len(result.entitlement_ids) == 2

    links = db_session.execute(
        select(func.count())
        .select_from(StarterPrepaidFundingLink)
        .where(StarterPrepaidFundingLink.purchase_ref == str(purchase_id))
    ).scalar_one()
    assert links == 2

    for invoice_id in result.invoice_ids:
        invoice = db_session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.status is InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")
        assert invoice.invoice_number is not None
        assert invoice.invoice_number.startswith("PRE-")
        starter_link = db_session.execute(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice_id
            )
        ).scalar_one()
        assert starter_link.document_id is not None

    payment = db_session.get(Payment, result.payment_id)
    assert payment is not None
    assert payment.allocations == []

    entitlements = db_session.execute(
        select(func.count())
        .select_from(ServiceEntitlement)
        .where(ServiceEntitlement.subscription_id == purchase.subscription_id)
    ).scalar_one()
    assert entitlements >= 2

    # Funding route probe: the same binding answers True, a foreign
    # purchase identity refuses instead of falling back.
    assert starter_prepaid.purchase_has_funding_intent(
        db_session,
        intent_reference=intent.reference,
        purchase_ref=str(purchase_id),
        subscriber_id=purchase.account_id,
    )
    with pytest.raises(starter_prepaid.StarterPrepaidError):
        starter_prepaid.purchase_has_funding_intent(
            db_session,
            intent_reference=intent.reference,
            purchase_ref=str(uuid4()),
            subscriber_id=purchase.account_id,
        )
