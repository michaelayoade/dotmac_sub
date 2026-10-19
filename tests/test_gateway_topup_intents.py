"""Gateway top-up intent command-owner behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.billing import BillingAccount, Invoice, InvoiceStatus, TopupIntent
from app.models.idempotency import IdempotencyKey
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.models.subscriber import Reseller
from app.services import gateway_topup_intents as svc
from app.services.account_credit_deposits import AccountCreditDeposits
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_payment_provider

_REAL_MODULE_INTENT_OPEN = svc._open_module_invoice_intent


@pytest.fixture(autouse=True)
def _stub_module_intent_open(monkeypatch) -> list:
    """Unit lane has no mod_payments schema; record module-intent opens.

    The real cross-module behavior is proven on migrated PostgreSQL in
    tests/integration/test_gateway_invoice_intent_module_pg.py.
    """

    calls: list = []
    monkeypatch.setattr(
        svc, "_open_module_invoice_intent", lambda _db, intent: calls.append(intent)
    )
    return calls


def _patch_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_value",
        lambda _db, _domain, key: {
            "gateway_topup_intent_ttl_minutes": 30,
            "topup_min_amount": 1000,
            "topup_max_amount": 500000,
        }[key],
    )


def _context(scope: str, *, idempotency_key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor="pytest:gateway-intents",
        scope=scope,
        reason="Gateway intent behavior test",
        idempotency_key=idempotency_key,
    )


def _checkout_binding(db_session, provider_type: str = "paystack"):
    return enable_payment_provider(db_session, provider_type)["payments.intent.v1"]


def _create_deposit(
    db_session,
    subscriber,
    *,
    capability_binding_id: UUID,
    preview_fingerprint: str | None = None,
) -> svc.GatewayTopupIntentResult:
    account_id = subscriber.id
    if preview_fingerprint is None:
        preview_fingerprint = AccountCreditDeposits.preview(
            db_session,
            account_id=account_id,
            amount="5000.00",
            currency="NGN",
            minimum="1000.00",
            maximum="500000.00",
        ).fingerprint
    command = svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=account_id,
        requested_amount="5000.00",
        reference="gateway-deposit-test-ref",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        expected_preview_fingerprint=preview_fingerprint,
        capability_binding_id=capability_binding_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    return svc.create_customer_gateway_topup_intent(
        db_session,
        command,
        context=_context(
            svc.CREATE_CUSTOMER_SCOPE,
            idempotency_key="stable-deposit-test-key",
        ),
    )


def test_customer_invoice_creation_derives_locked_invoice_amount(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-GATEWAY-OWNER",
        currency="NGN",
        subtotal=Decimal("3750.00"),
        total=Decimal("3750.00"),
        balance_due=Decimal("3750.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()
    invoice_id = invoice.id
    account_id = subscriber.id
    binding = _checkout_binding(db_session)
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.invoice_payment,
            account_id=account_id,
            invoice_id=invoice_id,
            reference="gateway-invoice-test-ref",
            provider_type="paystack",
            provider_id=None,
            created_by="pytest",
            capability_binding_id=binding_id,
        ),
        context=_context(svc.CREATE_CUSTOMER_SCOPE),
    )

    assert result.requested_amount == Decimal("3750.00")
    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None
    assert intent.invoice_id == invoice_id
    assert intent.metadata_ == {
        "payment_flow": "invoice_payment",
        "invoice_id": str(invoice_id),
        "invoice_number": "INV-GATEWAY-OWNER",
        "account_id": str(account_id),
        "capability_binding_id": str(binding_id),
    }


def test_customer_invoice_creation_rejects_proforma_at_owner_boundary(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="PF-GATEWAY-OWNER",
        currency="NGN",
        subtotal=Decimal("3750.00"),
        total=Decimal("3750.00"),
        balance_due=Decimal("3750.00"),
        status=InvoiceStatus.issued,
        is_proforma=True,
    )
    db_session.add(invoice)
    db_session.commit()
    binding = _checkout_binding(db_session)
    account_id = subscriber.id
    invoice_id = invoice.id
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(svc.GatewayTopupIntentError) as rejected:
        svc.create_customer_gateway_topup_intent(
            db_session,
            svc.CreateCustomerGatewayTopupIntentCommand(
                flow=svc.CustomerGatewayTopupFlow.invoice_payment,
                account_id=account_id,
                invoice_id=invoice_id,
                reference="gateway-proforma-test-ref",
                provider_type="paystack",
                provider_id=None,
                created_by="pytest",
                capability_binding_id=binding_id,
            ),
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )

    assert rejected.value.code.endswith("invoice_not_payable")
    assert (
        db_session.scalar(
            select(TopupIntent).where(
                TopupIntent.reference == "gateway-proforma-test-ref"
            )
        )
        is None
    )


def test_customer_deposit_creation_uses_policy_and_replays(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)

    first = _create_deposit(
        db_session,
        subscriber,
        capability_binding_id=binding.id,
    )
    second = _create_deposit(
        db_session,
        subscriber,
        capability_binding_id=binding.id,
        preview_fingerprint=first.preview_fingerprint,
    )

    assert first.intent_id == second.intent_id
    assert first.preview_fingerprint
    assert second.replayed is True
    assert first.requested_amount == Decimal("5000.00")


def test_customer_deposit_creation_requires_reviewed_preview(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)

    command = svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=subscriber.id,
        requested_amount="5000.00",
        reference="gateway-deposit-missing-preview",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        capability_binding_id=binding.id,
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(svc.GatewayTopupIntentError) as exc_info:
        svc.create_customer_gateway_topup_intent(
            db_session,
            command,
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )

    assert exc_info.value.code.endswith("preview_required")


def test_period_purchase_intent_is_typed_and_links_exact_purchase(
    monkeypatch, db_session, subscriber, subscription
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)
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
        preview_fingerprint="a" * 64,
        policy_version=1,
        policy_snapshot={},
        idempotency_key="period-purchase-test",
        created_by="pytest",
        expires_at=now + timedelta(minutes=30),
    )
    db_session.add(purchase)
    db_session.commit()
    purchase_id = purchase.id
    account_id = subscriber.id
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.prepaid_period_purchase,
            account_id=account_id,
            purchase_id=purchase_id,
            reference="gateway-period-purchase-ref",
            provider_type="paystack",
            provider_id=None,
            created_by="pytest",
            expected_preview_fingerprint="a" * 64,
            capability_binding_id=binding_id,
        ),
        context=_context(
            svc.CREATE_CUSTOMER_SCOPE,
            idempotency_key="period-purchase-test",
        ),
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    linked = db_session.get(PrepaidPeriodPurchase, purchase_id)
    assert result.requested_amount == Decimal("16125.00")
    assert intent is not None
    assert intent.purpose == "prepaid_period_purchase"
    assert intent.allocation_policy == "selected_purchase_invoices_only"
    assert intent.credit_application_policy == "none"
    assert intent.metadata_["purchase_id"] == str(purchase_id)
    assert linked is not None
    assert linked.topup_intent_id == intent.id
    assert linked.status is PrepaidPeriodPurchaseStatus.payment_pending


def test_reseller_creation_locks_canonical_billing_account(monkeypatch, db_session):
    _patch_policy(monkeypatch)
    reseller = Reseller(name="Gateway Reseller", code="GATEWAY-OWNER")
    db_session.add(reseller)
    db_session.flush()
    billing_account = BillingAccount(
        reseller_id=reseller.id,
        name="Gateway Reseller Billing",
        currency="NGN",
        status="active",
        is_active=True,
    )
    db_session.add(billing_account)
    db_session.commit()
    reseller_id = reseller.id
    billing_account_id = billing_account.id
    binding = _checkout_binding(db_session)
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_reseller_gateway_topup_intent(
        db_session,
        svc.CreateResellerGatewayTopupIntentCommand(
            billing_account_id=billing_account_id,
            reseller_id=reseller_id,
            reference="gateway-reseller-test-ref",
            provider_type="paystack",
            provider_id=None,
            requested_amount="12000.00",
            capability_binding_id=binding_id,
            save_card=True,
        ),
        context=_context(svc.CREATE_RESELLER_SCOPE),
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert result.billing_account_id == billing_account_id
    assert intent is not None
    assert intent.metadata_["payment_flow"] == "reseller_consolidated"
    assert intent.metadata_["reseller_card_id"] == str(reseller_id)


def test_saved_card_failure_atomically_releases_retry_reservation(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)
    result = _create_deposit(db_session, subscriber, capability_binding_id=binding.id)
    reservation = IdempotencyKey(
        scope=svc.SavedCardChargeScope.account_credit_deposit.value,
        key="declined-charge-key",
        account_id=subscriber.id,
    )
    db_session.add(reservation)
    db_session.commit()
    reservation_id: UUID = reservation.id

    db_session_adapter.release_read_transaction(db_session)
    failure = svc.fail_saved_card_charge(
        db_session,
        svc.FailSavedCardChargeCommand(
            intent_id=result.intent_id,
            reservation_id=reservation_id,
            reservation_scope=svc.SavedCardChargeScope.account_credit_deposit,
        ),
        context=_context(svc.FAIL_SAVED_CARD_SCOPE),
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert failure.changed is True
    assert failure.reservation_released is True
    assert intent is not None and intent.status == "failed"
    assert db_session.get(IdempotencyKey, reservation_id) is None


def test_saved_card_failure_rolls_back_on_reservation_mismatch(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)
    result = _create_deposit(db_session, subscriber, capability_binding_id=binding.id)
    reservation = IdempotencyKey(
        scope=svc.SavedCardChargeScope.invoice.value,
        key="mismatched-charge-key",
        account_id=subscriber.id,
    )
    db_session.add(reservation)
    db_session.commit()
    reservation_id = reservation.id

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(svc.GatewayTopupIntentError) as exc_info:
        svc.fail_saved_card_charge(
            db_session,
            svc.FailSavedCardChargeCommand(
                intent_id=result.intent_id,
                reservation_id=reservation_id,
                reservation_scope=svc.SavedCardChargeScope.account_credit_deposit,
            ),
            context=_context(svc.FAIL_SAVED_CARD_SCOPE),
        )

    assert exc_info.value.code.endswith("reservation_mismatch")
    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None and intent.status == "pending"
    assert db_session.get(IdempotencyKey, reservation_id) is not None


def test_customer_creation_rejects_missing_checkout_binding(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    command = svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=subscriber.id,
        requested_amount="5000.00",
        reference="gateway-missing-binding-ref",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        capability_binding_id=UUID(int=0),
    )

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(svc.GatewayTopupIntentError) as exc_info:
        svc.create_customer_gateway_topup_intent(
            db_session,
            command,
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )

    assert exc_info.value.code.endswith("checkout_binding_unavailable")


def _invoice_checkout(db_session, subscriber, *, number: str, reference: str):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=number,
        currency="NGN",
        subtotal=Decimal("1200.00"),
        total=Decimal("1200.00"),
        balance_due=Decimal("1200.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()
    invoice_id = invoice.id
    account_id = subscriber.id
    binding_id = _checkout_binding(db_session).id

    db_session_adapter.release_read_transaction(db_session)
    return svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.invoice_payment,
            account_id=account_id,
            invoice_id=invoice_id,
            reference=reference,
            provider_type="paystack",
            provider_id=None,
            created_by="pytest",
            capability_binding_id=binding_id,
        ),
        context=_context(svc.CREATE_CUSTOMER_SCOPE),
    )


def test_invoice_creation_switch_off_never_touches_payments_module(
    monkeypatch, db_session, subscriber
):
    """D1 hold: switch OFF (the default) is the pre-#3680 legacy-only checkout.

    The real helper is restored and the module entry point raises on any use,
    so a missing mod_payments schema (or any module call) would fail here.
    """

    _patch_policy(monkeypatch)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("payments module must not be touched while OFF")

    monkeypatch.setattr(svc, "_open_module_invoice_intent", _REAL_MODULE_INTENT_OPEN)
    monkeypatch.setattr(svc, "open_payment_intent", _forbidden)

    result = _invoice_checkout(
        db_session, subscriber, number="INV-MODULE-OFF", reference="gateway-off-ref"
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None
    assert intent.reference == "gateway-off-ref"
    assert result.payment_flow == "invoice_payment"


def test_invoice_creation_switch_on_opens_module_intent_once(
    monkeypatch, db_session, subscriber, _stub_module_intent_open
):
    _patch_policy(monkeypatch)
    consulted: list[bool] = []

    def _switch_on(_db) -> bool:
        consulted.append(True)
        return True

    monkeypatch.setattr(svc, "starter_ledger_live_enabled", _switch_on)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MODULE-INTENT",
        currency="NGN",
        subtotal=Decimal("1200.00"),
        total=Decimal("1200.00"),
        balance_due=Decimal("1200.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()
    invoice_id = invoice.id
    account_id = subscriber.id
    binding_id = _checkout_binding(db_session).id

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.invoice_payment,
            account_id=account_id,
            invoice_id=invoice_id,
            reference="gateway-module-intent-ref",
            provider_type="paystack",
            provider_id=None,
            created_by="pytest",
            capability_binding_id=binding_id,
        ),
        context=_context(svc.CREATE_CUSTOMER_SCOPE),
    )

    assert consulted, "the D1 switch must be consulted before opening"
    assert [intent.id for intent in _stub_module_intent_open] == [result.intent_id]


def test_module_intent_open_maps_authorized_binding(monkeypatch):
    """The real helper presents the exact authorized binding to payments."""

    from dotmac_payments import PaymentPurpose

    captured = {}

    def _capture(_db, *, scope, command):
        captured["scope"] = scope
        captured["command"] = command

    monkeypatch.setattr(svc, "open_payment_intent", _capture)
    opened = datetime(2026, 10, 17, 12, tzinfo=UTC)
    intent = TopupIntent(
        account_id=UUID(int=7),
        invoice_id=UUID(int=9),
        reference="module-open-ref",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("120.00"),
        channel="customer_selfcare",
        created_at=opened,
        expires_at=opened + timedelta(minutes=30),
    )
    _REAL_MODULE_INTENT_OPEN(None, intent)

    command = captured["command"]
    assert command.reference == "module-open-ref"
    assert command.payer_reference == str(UUID(int=7))
    assert command.purpose is PaymentPurpose.INVOICE_SETTLEMENT
    assert command.target_reference == str(UUID(int=9))
    assert command.requested.amount == Decimal("120.00")
    assert command.requested.currency.code == "NGN"
    assert command.requested.currency.minor_units == 2
    assert command.provider_type == "paystack"
    assert command.channel == "customer_selfcare"
    assert command.opened_at == opened
    assert command.expires_at == opened + timedelta(minutes=30)
