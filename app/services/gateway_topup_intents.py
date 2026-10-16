"""Typed root commands for durable gateway checkout intent lifecycle."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from dotmac_payments import (
    Conflict as PaymentsConflict,
)
from dotmac_payments import (
    GetPaymentIntentV1,
    OpenPaymentIntent,
    PaymentPurpose,
    get_payment_intent_v1,
    open_payment_intent,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import BillingAccount, Invoice, InvoiceStatus, TopupIntent
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallationState,
)
from app.models.service_period_purchase import (
    PrepaidPeriodPurchase,
    PrepaidPeriodPurchaseStatus,
)
from app.services import topup_intents
from app.services.account_credit_deposits import (
    SUPPORTED_CURRENCY,
    AccountCreditDeposits,
    DepositEligibilityError,
)
from app.services.billing._common import lock_account
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.locking import lock_for_update
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value

CREATE_CUSTOMER_SCOPE = "gateway-topup-intent:create-customer"
CREATE_RESELLER_SCOPE = "gateway-topup-intent:create-reseller"
FAIL_SAVED_CARD_SCOPE = "gateway-topup-intent:fail-saved-card"

_CREATE_CUSTOMER_COMMAND = OwnerCommandDefinition(
    owner="financial.gateway_topup_intent_commands",
    concern="customer gateway top-up intent creation coordination",
    name="create_customer_gateway_topup_intent",
)
_CREATE_RESELLER_COMMAND = OwnerCommandDefinition(
    owner="financial.gateway_topup_intent_commands",
    concern="reseller gateway top-up intent creation coordination",
    name="create_reseller_gateway_topup_intent",
)
_FAIL_SAVED_CARD_COMMAND = OwnerCommandDefinition(
    owner="financial.gateway_topup_intent_commands",
    concern="saved-card charge failure coordination",
    name="fail_saved_card_charge",
)

_MINIMUM_SETTING = "topup_min_amount"
_MAXIMUM_SETTING = "topup_max_amount"
_TTL_SETTING = "gateway_topup_intent_ttl_minutes"
_PAYMENT_INTENT_CAPABILITY = "payments.intent.v1"


class CustomerGatewayTopupFlow(str, Enum):
    """Customer gateway checkouts admitted by the coordinator."""

    invoice_payment = "invoice_payment"
    account_credit_deposit = "account_credit_deposit"
    prepaid_period_purchase = "prepaid_period_purchase"


class SavedCardChargeScope(str, Enum):
    """Canonical reservation scopes eligible for failure release."""

    invoice = "invoice_saved_card_charge"
    account_credit_deposit = "topup_saved_card_charge"
    prepaid_period_purchase = "prepaid_period_purchase_saved_card_charge"


class GatewayTopupIntentError(DomainError, ValueError):
    """Stable rejection from the gateway checkout coordinator."""


def _error(suffix: str, message: str, **details: object) -> GatewayTopupIntentError:
    return GatewayTopupIntentError(
        code=f"financial.gateway_topup_intent_commands.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CreateCustomerGatewayTopupIntentCommand:
    """Authenticated customer checkout request admitted by an adapter."""

    flow: CustomerGatewayTopupFlow
    account_id: UUID
    reference: str
    provider_type: str
    provider_id: UUID | None
    created_by: str
    capability_binding_id: UUID
    requested_amount: Decimal | int | float | str | None = None
    invoice_id: UUID | None = None
    payment_method_id: UUID | None = None
    expected_preview_fingerprint: str | None = None
    purchase_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateResellerGatewayTopupIntentCommand:
    """Authenticated reseller checkout request admitted by an adapter."""

    billing_account_id: UUID
    reseller_id: UUID
    reference: str
    provider_type: str
    provider_id: UUID | None
    requested_amount: Decimal | int | float | str
    capability_binding_id: UUID
    payment_method_id: UUID | None = None
    save_card: bool = False
    login_subscriber_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FailSavedCardChargeCommand:
    """Intent and optional charge reservation to fail atomically."""

    intent_id: UUID
    reservation_id: UUID | None
    reservation_scope: SavedCardChargeScope | None = None


@dataclass(frozen=True, slots=True)
class GatewayTopupIntentResult:
    """Immutable gateway intent result serialized only by adapters."""

    intent_id: UUID
    account_id: UUID | None
    billing_account_id: UUID | None
    reference: str
    provider_type: str
    currency: str
    requested_amount: Decimal
    expires_at: datetime
    payment_flow: str
    preview_fingerprint: str | None
    replayed: bool
    invoice_routing: topup_intents.InvoiceGatewayRoutingEvidence | None = None


@dataclass(frozen=True, slots=True)
class SavedCardFailureResult:
    """Atomic failure and retry-reservation release outcome."""

    intent_id: UUID
    changed: bool
    reservation_released: bool


def _configured_integer(db: Session, key: str) -> int:
    value = resolve_value(db, SettingDomain.billing, key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _error(
            "configuration_invalid",
            "Gateway top-up policy configuration is invalid",
            setting=key,
        )
    try:
        normalized = int(value)
    except ValueError as exc:
        raise _error(
            "configuration_invalid",
            "Gateway top-up policy configuration is invalid",
            setting=key,
        ) from exc
    if normalized <= 0:
        raise _error(
            "configuration_invalid",
            "Gateway top-up policy configuration must be positive",
            setting=key,
        )
    return normalized


def _amount(value: Decimal | int | float | str | None) -> Decimal:
    try:
        amount = round_money(to_decimal(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error("amount_invalid", "Gateway checkout amount is invalid") from exc
    if amount <= Decimal("0.00"):
        raise _error("amount_invalid", "Gateway checkout amount must be positive")
    return amount


def _require_checkout_binding(
    db: Session,
    *,
    provider_type: str,
    capability_binding_id: UUID,
) -> IntegrationCapabilityBinding:
    binding = db.get(IntegrationCapabilityBinding, capability_binding_id)
    if (
        binding is None
        or binding.capability_id != _PAYMENT_INTENT_CAPABILITY
        or binding.state != IntegrationBindingState.enabled.value
        or binding.installation.state != IntegrationInstallationState.enabled.value
        or binding.installation.connector_key != provider_type.strip().lower()
    ):
        raise _error(
            "checkout_binding_unavailable",
            "An enabled checkout capability binding is required",
        )
    return binding


def _deposit_idempotency_key(context: CommandContext, account_id: UUID) -> str:
    source = context.idempotency_key or str(context.command_id)
    digest = hashlib.sha256(f"{account_id}:{source}".encode()).hexdigest()
    return f"gateway-deposit-{digest}"


def _result(
    *,
    intent: TopupIntent,
    payment_flow: str,
    preview_fingerprint: str | None,
    replayed: bool,
    invoice_routing: topup_intents.InvoiceGatewayRoutingEvidence | None = None,
) -> GatewayTopupIntentResult:
    if intent.expires_at is None:
        raise _error(
            "record_invalid",
            "Gateway intent expiry evidence is incomplete",
            intent_id=str(intent.id),
        )
    return GatewayTopupIntentResult(
        intent_id=intent.id,
        account_id=intent.account_id,
        billing_account_id=intent.billing_account_id,
        reference=intent.reference,
        provider_type=intent.provider_type,
        currency=intent.currency,
        requested_amount=intent.requested_amount,
        expires_at=_utc(intent.expires_at),
        payment_flow=payment_flow,
        preview_fingerprint=preview_fingerprint,
        replayed=replayed,
        invoice_routing=invoice_routing,
    )


def create_customer_gateway_topup_intent(
    db: Session,
    command: CreateCustomerGatewayTopupIntentCommand,
    *,
    context: CommandContext,
) -> GatewayTopupIntentResult:
    """Create one customer gateway checkout trace in a root transaction."""

    return execute_owner_command(
        db,
        definition=_CREATE_CUSTOMER_COMMAND,
        context=context,
        operation=lambda: _create_customer_gateway_topup_intent(
            db,
            command=command,
            context=context,
        ),
    )


def _exact_invoice_money(amount: Decimal, currency: str) -> Money:
    """Refuse loss before constructing quantizing Money or the legacy projection."""
    if (
        not amount.is_finite()
        or not Decimal(0) < amount < Decimal("10000000000")
        or amount != amount.quantize(Decimal("0.01"))
    ):
        raise _error(
            "amount_invalid",
            "Invoice amount cannot be represented exactly at its storage scale",
        )
    try:
        return Money(amount, Currency(currency, 2))
    except MoneyError as exc:
        raise _error("amount_invalid", "Invoice amount or currency is invalid") from exc


def _create_customer_gateway_topup_intent(
    db: Session,
    *,
    command: CreateCustomerGatewayTopupIntentCommand,
    context: CommandContext,
) -> GatewayTopupIntentResult:
    if command.flow is CustomerGatewayTopupFlow.invoice_payment:
        if (
            command.invoice_id is None
            or command.requested_amount is not None
            or command.purchase_id is not None
        ):
            raise _error(
                "flow_evidence_invalid",
                "Invoice checkout requires an invoice and no caller amount",
            )
        created_by = command.created_by.strip()
        reference = command.reference.strip()
        provider_type = command.provider_type.strip().lower()
        if not created_by:
            raise _error("created_by_required", "Gateway checkout actor is required")
        if not reference or not provider_type:
            raise _error(
                "flow_evidence_invalid",
                "Invoice checkout reference and provider are required",
            )
        lock_account(db, str(command.account_id))
        invoice = db.scalar(
            select(Invoice)
            .where(Invoice.id == command.invoice_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invoice is None or invoice.account_id != command.account_id:
            raise _error("invoice_not_found", "Invoice was not found for this account")
        local = db.scalar(
            select(TopupIntent)
            .where(TopupIntent.reference == reference)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        scope = TenantScope(OPERATOR_TENANT_ID)
        try:
            bound = get_payment_intent_v1(
                db, scope=scope, query=GetPaymentIntentV1(reference)
            )
            if (local is None) != (bound is None):
                raise _error(
                    "invoice_binding_incomplete",
                    "Invoice reference has incomplete module/projection evidence; reconstruction is refused",
                )
            _require_checkout_binding(
                db,
                provider_type=provider_type,
                capability_binding_id=command.capability_binding_id,
            )
            if local is not None:
                number = (local.metadata_ or {}).get("invoice_number")
                if not isinstance(number, str):
                    raise _error(
                        "record_invalid",
                        "Invoice checkout display provenance is incomplete",
                    )
            else:
                number = invoice.invoice_number or ""
            routing = topup_intents.InvoiceGatewayRoutingEvidence(
                account_id=command.account_id,
                invoice_id=invoice.id,
                invoice_number=number,
                provider_type=provider_type,
                provider_id=command.provider_id,
                capability_binding_id=command.capability_binding_id,
                payment_method_id=command.payment_method_id,
                created_by=created_by,
            )
            if bound is None:
                if invoice.is_proforma or invoice.status in {
                    InvoiceStatus.draft,
                    InvoiceStatus.paid,
                    InvoiceStatus.void,
                    InvoiceStatus.written_off,
                }:
                    raise _error(
                        "invoice_not_payable",
                        "Invoice is no longer payable by gateway checkout",
                    )
                requested = _exact_invoice_money(
                    invoice.balance_due
                    if invoice.balance_due is not None
                    else invoice.total,
                    invoice.currency,
                )
                opened_at = datetime.now(UTC)
                module_command = OpenPaymentIntent(
                    reference=reference,
                    payer_reference=str(command.account_id),
                    target_reference=str(invoice.id),
                    purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                    requested=requested,
                    provider_type=provider_type,
                    channel=topup_intents.TopupIntentChannel.customer_selfcare.value,
                    opened_at=opened_at,
                    expires_at=opened_at
                    + timedelta(minutes=_configured_integer(db, _TTL_SETTING)),
                )
            else:
                # Validate before replaying. Terminal local lifecycle and later
                # reconciliation metadata are retained, never reset or replaced.
                topup_intents.stage_invoice_gateway_projection(
                    db, binding=bound, routing=routing, existing=local
                )
                module_command = OpenPaymentIntent(
                    reference=bound.reference,
                    payer_reference=bound.payer_reference,
                    target_reference=bound.target_reference,
                    purpose=bound.purpose,
                    requested=bound.requested,
                    provider_type=bound.provider_type,
                    channel=bound.channel,
                    opened_at=bound.opened_at,
                    expires_at=bound.expires_at,
                )
            # Do not expose the module ORM return across the owner boundary.
            open_payment_intent(db, scope=scope, command=module_command)
            frozen = get_payment_intent_v1(
                db, scope=scope, query=GetPaymentIntentV1(reference)
            )
            if frozen is None:
                raise _error(
                    "invoice_binding_conflict",
                    "Module did not retain the invoice checkout binding",
                )
            staged = topup_intents.stage_invoice_gateway_projection(
                db, binding=frozen, routing=routing, existing=local
            )
            if not staged.replayed:
                topup_intents.stage_gateway_topup_intent_created_event(
                    db, intent=staged.intent, context=context
                )
            return _result(
                intent=staged.intent,
                payment_flow=command.flow.value,
                preview_fingerprint=None,
                replayed=staged.replayed,
                invoice_routing=routing,
            )
        except (PaymentsConflict, topup_intents.TopupIntentError) as exc:
            raise _error(
                "invoice_binding_conflict",
                "Invoice reference is linked to different binding or routing evidence",
            ) from exc
        except IntegrityError as exc:
            # No recovery or further statements on an aborted transaction. The
            # existing root executor rolls back module/projection/event together.
            raise _error(
                "intent_conflict", "Invoice reference is already being created"
            ) from exc
    _require_checkout_binding(
        db,
        provider_type=command.provider_type,
        capability_binding_id=command.capability_binding_id,
    )
    created_by = command.created_by.strip()
    if not created_by:
        raise _error("created_by_required", "Gateway checkout actor is required")
    expires_at = datetime.now(UTC) + timedelta(
        minutes=_configured_integer(db, _TTL_SETTING)
    )

    if command.flow is CustomerGatewayTopupFlow.account_credit_deposit:
        if command.invoice_id is not None:
            raise _error(
                "flow_evidence_invalid",
                "Account-credit checkout cannot carry an invoice identity",
            )
        expected_preview_fingerprint = str(
            command.expected_preview_fingerprint or ""
        ).strip()
        if len(expected_preview_fingerprint) != 64:
            raise _error(
                "preview_required",
                "Review the latest account-credit allocation preview before checkout",
            )
        minimum = _configured_integer(db, _MINIMUM_SETTING)
        maximum = _configured_integer(db, _MAXIMUM_SETTING)
        if maximum < minimum:
            raise _error(
                "configuration_invalid",
                "Gateway top-up maximum is below the minimum",
                minimum=minimum,
                maximum=maximum,
            )
        try:
            intent, preview, replayed = AccountCreditDeposits.stage_intent(
                db,
                account_id=command.account_id,
                amount=_amount(command.requested_amount),
                currency=SUPPORTED_CURRENCY,
                minimum=minimum,
                maximum=maximum,
                reference=command.reference,
                provider_type=command.provider_type,
                provider_id=command.provider_id,
                capability_binding_id=command.capability_binding_id,
                expires_at=expires_at,
                idempotency_key=_deposit_idempotency_key(context, command.account_id),
                channel=topup_intents.TopupIntentChannel.customer_selfcare,
                created_by=created_by,
                expected_preview_fingerprint=expected_preview_fingerprint,
                metadata={
                    "payment_flow": CustomerGatewayTopupFlow.account_credit_deposit.value,
                    **(
                        {"payment_method_id": str(command.payment_method_id)}
                        if command.payment_method_id is not None
                        else {}
                    ),
                },
            )
        except DepositEligibilityError as exc:
            raise _error(
                "deposit_rejected",
                str(exc),
                deposit_code=exc.code,
            ) from exc
        except IntegrityError as exc:
            raise _error(
                "intent_conflict",
                "Gateway deposit intent is already being created",
            ) from exc
        staged = topup_intents.StagedGatewayTopupIntent(
            intent=intent,
            replayed=replayed,
        )
        preview_fingerprint = preview.fingerprint
    else:
        if (
            command.purchase_id is None
            or command.invoice_id is not None
            or command.requested_amount is not None
        ):
            raise _error(
                "flow_evidence_invalid",
                "Period purchase checkout requires only a purchase identity",
            )
        lock_account(db, str(command.account_id))
        purchase = lock_for_update(db, PrepaidPeriodPurchase, command.purchase_id)
        if purchase is None or purchase.account_id != command.account_id:
            raise _error(
                "purchase_not_found",
                "Service-period purchase was not found for this account",
            )
        if purchase.topup_intent_id is not None:
            existing = lock_for_update(db, TopupIntent, purchase.topup_intent_id)
            if (
                existing is None
                or existing.account_id != command.account_id
                or existing.provider_id != command.provider_id
                or existing.provider_type != command.provider_type
                or existing.capability_binding_id != command.capability_binding_id
                or (existing.metadata_ or {}).get("payment_method_id")
                != (
                    str(command.payment_method_id)
                    if command.payment_method_id
                    else None
                )
                or command.expected_preview_fingerprint != purchase.preview_fingerprint
            ):
                raise _error(
                    "purchase_intent_conflict",
                    "Purchase already names a different checkout.",
                )
            return _result(
                intent=existing,
                payment_flow=command.flow.value,
                preview_fingerprint=purchase.preview_fingerprint,
                replayed=True,
            )
        if _utc(purchase.expires_at) < datetime.now(UTC):
            raise _error(
                "purchase_not_payable",
                "Purchase quote has expired; review a new quote.",
            )
        if purchase.status not in {
            PrepaidPeriodPurchaseStatus.quoted,
            PrepaidPeriodPurchaseStatus.payment_pending,
        }:
            raise _error(
                "purchase_not_payable",
                "Service-period purchase is no longer payable",
                status=purchase.status.value,
            )
        expected = str(command.expected_preview_fingerprint or "").strip()
        if expected != purchase.preview_fingerprint:
            raise _error(
                "preview_required",
                "Review the latest service-period quote before checkout",
            )
        staged = topup_intents.stage_gateway_topup_intent(
            db,
            topup_intents.StageGatewayTopupIntentCommand(
                flow=topup_intents.GatewayTopupIntentFlow.prepaid_period_purchase,
                account_id=command.account_id,
                purchase_id=purchase.id,
                reference=command.reference,
                provider_type=command.provider_type,
                provider_id=command.provider_id,
                capability_binding_id=command.capability_binding_id,
                currency=purchase.currency,
                requested_amount=purchase.total,
                expires_at=min(expires_at, _utc(purchase.expires_at)),
                payment_method_id=command.payment_method_id,
                channel=topup_intents.TopupIntentChannel.customer_selfcare,
                created_by=created_by,
                preview_fingerprint=purchase.preview_fingerprint,
                idempotency_key=purchase.idempotency_key,
            ),
            context=context,
        )
        if purchase.topup_intent_id not in {None, staged.intent.id}:
            raise _error(
                "purchase_intent_conflict",
                "Service-period purchase is linked to another checkout",
            )
        purchase.topup_intent_id = staged.intent.id
        purchase.status = PrepaidPeriodPurchaseStatus.payment_pending
        preview_fingerprint = purchase.preview_fingerprint

    if not staged.replayed:
        topup_intents.stage_gateway_topup_intent_created_event(
            db,
            intent=staged.intent,
            context=context,
        )
    return _result(
        intent=staged.intent,
        payment_flow=command.flow.value,
        preview_fingerprint=preview_fingerprint,
        replayed=staged.replayed,
    )


def create_reseller_gateway_topup_intent(
    db: Session,
    command: CreateResellerGatewayTopupIntentCommand,
    *,
    context: CommandContext,
) -> GatewayTopupIntentResult:
    """Create one reseller consolidated gateway trace in a root transaction."""

    return execute_owner_command(
        db,
        definition=_CREATE_RESELLER_COMMAND,
        context=context,
        operation=lambda: _create_reseller_gateway_topup_intent(
            db,
            command=command,
            context=context,
        ),
    )


def _create_reseller_gateway_topup_intent(
    db: Session,
    *,
    command: CreateResellerGatewayTopupIntentCommand,
    context: CommandContext,
) -> GatewayTopupIntentResult:
    _require_checkout_binding(
        db,
        provider_type=command.provider_type,
        capability_binding_id=command.capability_binding_id,
    )
    billing_account = lock_for_update(db, BillingAccount, command.billing_account_id)
    if (
        billing_account is None
        or billing_account.reseller_id != command.reseller_id
        or not billing_account.is_active
    ):
        raise _error(
            "billing_account_unavailable",
            "Active reseller billing account was not found",
            billing_account_id=str(command.billing_account_id),
        )
    try:
        staged = topup_intents.stage_gateway_topup_intent(
            db,
            topup_intents.StageGatewayTopupIntentCommand(
                flow=topup_intents.GatewayTopupIntentFlow.reseller_consolidated,
                billing_account_id=billing_account.id,
                reseller_id=command.reseller_id,
                reference=command.reference,
                provider_type=command.provider_type,
                provider_id=command.provider_id,
                capability_binding_id=command.capability_binding_id,
                currency=billing_account.currency,
                requested_amount=_amount(command.requested_amount),
                expires_at=datetime.now(UTC)
                + timedelta(minutes=_configured_integer(db, _TTL_SETTING)),
                payment_method_id=command.payment_method_id,
                save_card=command.save_card,
                login_subscriber_id=command.login_subscriber_id,
                channel=topup_intents.TopupIntentChannel.reseller_selfcare,
                created_by=str(command.reseller_id),
            ),
            context=context,
        )
    except IntegrityError as exc:
        raise _error(
            "intent_conflict",
            "Reseller gateway intent is already being created",
        ) from exc
    if not staged.replayed:
        topup_intents.stage_gateway_topup_intent_created_event(
            db,
            intent=staged.intent,
            context=context,
        )
    return _result(
        intent=staged.intent,
        payment_flow=topup_intents.GatewayTopupIntentFlow.reseller_consolidated.value,
        preview_fingerprint=None,
        replayed=staged.replayed,
    )


def fail_saved_card_charge(
    db: Session,
    command: FailSavedCardChargeCommand,
    *,
    context: CommandContext,
) -> SavedCardFailureResult:
    """Fail an intent and release its unused charge key in one transaction."""

    return execute_owner_command(
        db,
        definition=_FAIL_SAVED_CARD_COMMAND,
        context=context,
        operation=lambda: _fail_saved_card_charge(
            db,
            command=command,
            context=context,
        ),
    )


def _fail_saved_card_charge(
    db: Session,
    *,
    command: FailSavedCardChargeCommand,
    context: CommandContext,
) -> SavedCardFailureResult:
    projection = topup_intents.stage_topup_intent_failure(
        db,
        topup_intents.FailTopupIntentCommand(
            intent_id=command.intent_id,
            source=topup_intents.TopupIntentFailureSource.saved_card_charge,
            reason=topup_intents.TopupIntentFailureReason.gateway_charge_failed,
        ),
        context=context,
    )
    released = False
    if command.reservation_id is not None:
        reservation = lock_for_update(db, IdempotencyKey, command.reservation_id)
        if reservation is not None:
            intent = lock_for_update(db, TopupIntent, command.intent_id)
            if (
                intent is None
                or intent.account_id is None
                or reservation.account_id != intent.account_id
                or command.reservation_scope is None
                or reservation.scope != command.reservation_scope.value
                or reservation.ref_id is not None
            ):
                raise _error(
                    "reservation_mismatch",
                    "Saved-card reservation does not match the failed intent",
                    reservation_id=str(reservation.id),
                )
            db.delete(reservation)
            released = True
    return SavedCardFailureResult(
        intent_id=projection.intent_id,
        changed=projection.changed,
        reservation_released=released,
    )
