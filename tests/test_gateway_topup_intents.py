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
from tests.payment_invoice_intent_helpers import (
    _invoice_module_stub_fixture,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("invoice_module_stub")


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


@pytest.fixture
def invoice_command(
    monkeypatch, db_session, subscriber
) -> svc.CreateCustomerGatewayTopupIntentCommand:
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MODULE-BINDING",
        currency="NGN",
        subtotal=Decimal("3750.00"),
        total=Decimal("3750.00"),
        balance_due=Decimal("3750.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()
    binding = _checkout_binding(db_session)
    return svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.invoice_payment,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        reference="prospective-module-binding",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        capability_binding_id=binding.id,
    )


def _create_invoice(
    db_session, command: svc.CreateCustomerGatewayTopupIntentCommand
) -> svc.GatewayTopupIntentResult:
    db_session_adapter.release_read_transaction(db_session)
    return svc.create_customer_gateway_topup_intent(
        db_session, command, context=_context(svc.CREATE_CUSTOMER_SCOPE)
    )


def test_invoice_four_existence_states(
    db_session, invoice_command, invoice_module_stub
):
    from dataclasses import replace
    from uuid import uuid4

    first = _create_invoice(db_session, invoice_command)
    assert first.replayed is False
    assert first.intent_id == invoice_module_stub[first.reference].intent_id
    local = db_session.get(TopupIntent, first.intent_id)
    assert local.module_intent_id == first.intent_id
    assert local.module_intent_tenant_id == svc.OPERATOR_TENANT_ID
    assert _create_invoice(db_session, invoice_command).replayed is True
    # Module-only references cannot reconstruct routing/lifecycle evidence.
    module_only = replace(invoice_command, reference="module-only-reference")
    invoice_module_stub[module_only.reference] = replace(
        invoice_module_stub[first.reference],
        reference=module_only.reference,
        intent_id=uuid4(),
    )
    with pytest.raises(svc.GatewayTopupIntentError, match="reconstruction is refused"):
        _create_invoice(db_session, module_only)
    # A legacy-only reference cannot silently acquire new module authority.
    del invoice_module_stub[first.reference]
    with pytest.raises(svc.GatewayTopupIntentError, match="reconstruction is refused"):
        _create_invoice(db_session, invoice_command)


def test_invoice_replay_retains_original_money_times_terminal_and_metadata(
    monkeypatch, db_session, invoice_command, invoice_module_stub
):
    first = _create_invoice(db_session, invoice_command)
    original = invoice_module_stub[first.reference]
    local = db_session.get(TopupIntent, first.intent_id)
    local.status = "expired"
    local.metadata_ = {**local.metadata_, "gateway_reconciliation": "later-observation"}
    invoice = db_session.get(Invoice, invoice_command.invoice_id)
    invoice.balance_due = Decimal("1.00")
    invoice.status = InvoiceStatus.paid
    invoice.invoice_number = "RENAMED-DISPLAY"
    db_session.commit()
    monkeypatch.setattr(
        svc,
        "_configured_integer",
        lambda *_args: pytest.fail("Replay must not read current TTL"),
    )
    second = _create_invoice(db_session, invoice_command)
    assert second.replayed is True
    assert second.requested_amount == first.requested_amount
    assert second.expires_at == first.expires_at
    assert second.invoice_routing.invoice_number == "INV-MODULE-BINDING"
    assert invoice_module_stub[first.reference] == original
    db_session.refresh(local)
    assert local.status == "expired"
    assert local.metadata_["gateway_reconciliation"] == "later-observation"


@pytest.mark.parametrize(
    "field",
    (
        "payer",
        "target",
        "purpose",
        "amount",
        "currency",
        "scale",
        "provider",
        "channel",
        "opened",
        "expiry",
        "reference",
        "identity",
        "tenant",
    ),
)
def test_invoice_replay_refuses_each_binding_drift(
    db_session, invoice_command, invoice_module_stub, field: str
):
    from dataclasses import replace
    from uuid import uuid4

    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.money import Currency, Money
    from dotmac_payments import PaymentPurpose

    first = _create_invoice(db_session, invoice_command)
    bound = invoice_module_stub[first.reference]
    match field:
        case "payer":
            bound = replace(bound, payer_reference=str(uuid4()))
        case "target":
            bound = replace(bound, target_reference=None)
        case "purpose":
            bound = replace(bound, purpose=PaymentPurpose.ACCOUNT_CREDIT_DEPOSIT)
        case "amount":
            bound = replace(
                bound, requested=Money(Decimal("3750.01"), Currency("NGN", 2))
            )
        case "currency":
            bound = replace(
                bound, requested=Money(Decimal("3750.00"), Currency("USD", 2))
            )
        case "scale":
            bound = replace(
                bound, requested=Money(Decimal("3750.00"), Currency("NGN", 3))
            )
        case "provider":
            bound = replace(bound, provider_type="FLUTTERWAVE")
        case "channel":
            bound = replace(bound, channel="RESELLER_SELFCARE")
        case "opened":
            bound = replace(bound, opened_at=bound.opened_at + timedelta(seconds=1))
        case "expiry":
            bound = replace(bound, expires_at=None)
        case "reference":
            bound = replace(bound, reference="another-reference")
        case "identity":
            bound = replace(bound, intent_id=uuid4())
        case "tenant":
            bound = replace(bound, scope=TenantScope(uuid4()))
    invoice_module_stub[first.reference] = bound
    with pytest.raises(svc.GatewayTopupIntentError) as refusal:
        _create_invoice(db_session, invoice_command)
    assert refusal.value.code.endswith("invoice_binding_conflict")


@pytest.mark.parametrize(
    "field",
    (
        "provider_id",
        "capability_binding_id",
        "payment_method_id",
        "created_by",
        "invoice_id",
        "metadata_target",
        "module_link",
        "module_tenant",
    ),
)
def test_invoice_replay_refuses_projection_routing_drift(
    db_session, invoice_command, invoice_module_stub, field: str
):
    from uuid import uuid4

    first = _create_invoice(db_session, invoice_command)
    local = db_session.get(TopupIntent, first.intent_id)
    match field:
        case "provider_id":
            local.provider_id = uuid4()
        case "capability_binding_id":
            local.capability_binding_id = uuid4()
        case "payment_method_id":
            local.metadata_ = {**local.metadata_, "payment_method_id": str(uuid4())}
        case "created_by":
            local.created_by = "different-actor"
        case "invoice_id":
            local.invoice_id = uuid4()
        case "metadata_target":
            local.metadata_ = {**local.metadata_, "invoice_id": str(uuid4())}
        case "module_link":
            local.module_intent_id = uuid4()
        case "module_tenant":
            local.module_intent_tenant_id = uuid4()
    # Impossible persisted FK drift is exercised at the typed projection seam,
    # before any SQL; migrated PG separately proves the structural constraints.
    from unittest.mock import Mock

    from app.services import topup_intents

    with pytest.raises(topup_intents.TopupIntentError) as refusal:
        topup_intents.stage_invoice_gateway_projection(
            Mock(),
            binding=invoice_module_stub[first.reference],
            routing=first.invoice_routing,
            existing=local,
        )
    assert refusal.value.code.endswith("gateway_reference_conflict")
    db_session.rollback()


@pytest.mark.parametrize(
    "amount",
    (
        Decimal("0"),
        Decimal("1.001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("10000000000"),
    ),
)
def test_invoice_money_refuses_loss_before_money_construction(amount: Decimal):
    with pytest.raises(svc.GatewayTopupIntentError) as refusal:
        svc._exact_invoice_money(amount, "NGN")
    assert refusal.value.code.endswith("amount_invalid")


def test_retired_invoice_staging_refuses_before_any_database_access():
    from unittest.mock import Mock

    from app.services import topup_intents

    db = Mock()
    with pytest.raises(topup_intents.TopupIntentError) as refusal:
        topup_intents.stage_gateway_topup_intent(
            db,
            topup_intents.StageGatewayTopupIntentCommand(
                flow=topup_intents.GatewayTopupIntentFlow.invoice_payment,
                reference="retired",
                provider_type="paystack",
                currency="NGN",
                requested_amount=Decimal("1.00"),
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
            ),
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )
    assert refusal.value.code.endswith("gateway_invoice_binding_retired")
    assert db.mock_calls == []


def test_invoice_without_display_number_creates_and_replays(
    db_session, invoice_command
):
    invoice = db_session.get(Invoice, invoice_command.invoice_id)
    invoice.invoice_number = None
    db_session.commit()
    first = _create_invoice(db_session, invoice_command)
    second = _create_invoice(db_session, invoice_command)
    assert first.intent_id == second.intent_id
    assert (
        first.invoice_routing.invoice_number
        == second.invoice_routing.invoice_number
        == ""
    )
    assert second.replayed
    local = db_session.get(TopupIntent, first.intent_id)
    assert local.invoice_id == invoice_command.invoice_id
    assert local.metadata_["invoice_number"] == ""
