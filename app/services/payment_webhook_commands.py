"""Typed coordinator for payment consequences from verified inbox receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import PaymentProviderType, PaymentStatus, TopupIntent
from app.models.integration_platform import IntegrationInbox
from app.services import billing as billing_service
from app.services.account_credit_deposits import (
    SETTLEMENT_PARTICIPANT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    DepositEligibilityError,
    SettleAccountCreditDepositCommand,
)
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.integrations import inbox as integration_inbox
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.payment_provider_events import (
    WEBHOOK_PARTICIPANT_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEventResult,
)
from app.services.topup_intents import (
    COMPLETION_SCOPE,
    CompleteTopupIntentCommand,
    TopupIntentCompletionSource,
    TopupIntentError,
    stage_topup_intent_completion,
)

PROCESS_SCOPE = "payment-webhook:process-claimed-receipt"

_PROCESS_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_webhooks",
    concern="billing consequence submission from verified receipts",
    name="process_claimed_payment_webhook",
)


class PaymentWebhookProvider(StrEnum):
    """Provider adapters with a versioned normalization contract."""

    PAYSTACK = "paystack"
    FLUTTERWAVE = "flutterwave"


class PaymentWebhookError(DomainError, ValueError):
    """Stable rejection from verified payment-webhook processing."""


def _error(suffix: str, message: str, **details: object) -> PaymentWebhookError:
    return PaymentWebhookError(
        code=f"financial.payment_webhooks.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PaymentWebhookReceiptIdentity:
    """Provider receipt identity needed by the integration inbox."""

    provider: PaymentWebhookProvider
    provider_event_id: str
    event_type: str


@dataclass(frozen=True, slots=True)
class ProcessClaimedPaymentWebhookCommand:
    """Exact claimed receipt admitted by the provider adapter."""

    receipt_id: UUID
    provider: PaymentWebhookProvider


@dataclass(frozen=True, slots=True)
class ProcessedPaymentWebhook:
    """Immutable evidence returned after the full consequence commits."""

    receipt_id: UUID
    provider: PaymentWebhookProvider
    provider_event_id: UUID | None
    payment_id: UUID | None
    replayed: bool = False

    def consequence(self) -> dict[str, object]:
        return {
            "status": "ok",
            "http_status": 200,
            "provider_event_id": (
                str(self.provider_event_id) if self.provider_event_id else None
            ),
            "payment_id": str(self.payment_id) if self.payment_id else None,
        }


@dataclass(frozen=True, slots=True)
class _SettlementObservation:
    status: PaymentStatus
    amount: Decimal | None
    provider_fee: Decimal
    currency: str | None
    reference: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _PreparedPaymentWebhook:
    ingest: PaymentProviderEventCommand
    settlement: _SettlementObservation | None
    topup_intent: TopupIntent | None


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("payload_invalid", f"Payment webhook {field} must be an object")
    return value


def identify_verified_payment_webhook(
    provider: PaymentWebhookProvider,
    payload: Mapping[str, Any],
) -> PaymentWebhookReceiptIdentity:
    """Derive one stable receipt identity before inbox admission."""

    data = _mapping(payload.get("data", {}), field="data")
    event_type = str(payload.get("event") or "unknown").strip() or "unknown"
    reference_field = (
        "reference" if provider is PaymentWebhookProvider.PAYSTACK else "tx_ref"
    )
    identity = str(data.get(reference_field) or data.get("id") or "").strip()
    if not identity:
        raise _error(
            "payload_invalid",
            "Payment webhook omitted its provider event identity",
            provider=provider.value,
        )
    return PaymentWebhookReceiptIdentity(
        provider=provider,
        provider_event_id=f"{provider.value}-{identity}",
        event_type=event_type,
    )


def _money(value: object, *, field: str, divisor: Decimal | None = None) -> Decimal:
    try:
        amount = Decimal(str(value))
        if divisor is not None:
            amount /= divisor
        if not amount.is_finite():
            raise InvalidOperation
        return round_money(amount)
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise _error(
            "payload_invalid",
            f"Payment webhook {field} is not valid money",
            field=field,
        ) from exc


def _currency(value: object) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _error(
            "payload_invalid",
            "Successful payment webhook currency must be a three-letter code",
            field="currency",
        )
    return currency


def _settlement_observation(
    provider: PaymentWebhookProvider,
    *,
    event_type: str,
    data: Mapping[str, Any],
) -> _SettlementObservation | None:
    if provider is PaymentWebhookProvider.PAYSTACK:
        if event_type != "charge.success":
            return None
        metadata = data.get("metadata")
        return _SettlementObservation(
            status=PaymentStatus.succeeded,
            amount=_money(data.get("amount", 0), field="amount", divisor=Decimal(100)),
            provider_fee=_money(
                data.get("fees", 0), field="fees", divisor=Decimal(100)
            ),
            currency=_currency(data.get("currency")),
            reference=str(data.get("reference") or "").strip() or None,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )

    if event_type != "charge.completed":
        return None
    metadata = data.get("meta")
    status = str(data.get("status") or "").strip().lower()
    if status == "successful":
        return _SettlementObservation(
            status=PaymentStatus.succeeded,
            amount=_money(data.get("amount", 0), field="amount"),
            provider_fee=_money(data.get("app_fee", 0), field="app_fee"),
            currency=_currency(data.get("currency")),
            reference=str(data.get("tx_ref") or "").strip() or None,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
    if status == "failed":
        return _SettlementObservation(
            status=PaymentStatus.failed,
            amount=None,
            provider_fee=Decimal("0.00"),
            currency=None,
            reference=str(data.get("tx_ref") or "").strip() or None,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
    return None


def _optional_uuid(value: object) -> UUID | None:
    if value is None or value == "":
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _metadata_uuid(
    metadata: Mapping[str, Any],
    *,
    field: str,
    error_suffix: str,
) -> UUID | None:
    value = metadata.get(field)
    if value is None or value == "":
        return None
    parsed = _optional_uuid(value)
    if parsed is None:
        raise _error(
            error_suffix,
            f"Payment webhook {field} is not a valid identifier",
            field=field,
        )
    return parsed


def _resolve_topup_intent(
    db: Session,
    *,
    provider: PaymentWebhookProvider,
    settlement: _SettlementObservation,
) -> TopupIntent | None:
    metadata_intent_id = _metadata_uuid(
        settlement.metadata,
        field="topup_intent_id",
        error_suffix="topup_intent_mismatch",
    )
    if metadata_intent_id is not None:
        intent = db.get(TopupIntent, metadata_intent_id)
        if intent is None:
            raise _error(
                "topup_intent_mismatch",
                "Payment webhook top-up intent does not exist",
                intent_id=str(metadata_intent_id),
            )
    elif settlement.reference:
        intent = db.scalar(
            select(TopupIntent).where(TopupIntent.reference == settlement.reference)
        )
    else:
        intent = None
    if intent is None:
        return None
    if settlement.reference and intent.reference != settlement.reference:
        raise _error(
            "topup_intent_mismatch",
            "Payment webhook reference does not match the selected top-up intent",
            intent_id=str(intent.id),
        )
    if intent.provider_type != provider.value:
        raise _error(
            "topup_intent_mismatch",
            "Payment webhook provider does not match the selected top-up intent",
            intent_id=str(intent.id),
        )
    return intent


def _prepare_payment_webhook(
    db: Session,
    *,
    receipt: IntegrationInbox,
    provider: PaymentWebhookProvider,
    provider_id: UUID,
) -> _PreparedPaymentWebhook:
    payload = _mapping(receipt.payload_json, field="payload")
    data = _mapping(payload.get("data", {}), field="data")
    settlement = _settlement_observation(
        provider,
        event_type=receipt.event_type,
        data=data,
    )
    external_id = str(data.get("id") or "").strip() or None
    ingest = PaymentProviderEventCommand(
        provider_id=provider_id,
        event_type=receipt.event_type,
        external_id=external_id,
        idempotency_key=receipt.provider_event_id,
        payload=dict(payload),
    )
    if settlement is None:
        return _PreparedPaymentWebhook(
            ingest=ingest, settlement=None, topup_intent=None
        )

    topup_intent = _resolve_topup_intent(
        db,
        provider=provider,
        settlement=settlement,
    )
    if settlement.status != PaymentStatus.succeeded:
        return _PreparedPaymentWebhook(
            ingest=replace(ingest, observed_payment_status=settlement.status),
            settlement=settlement,
            topup_intent=topup_intent,
        )
    if settlement.amount is None or settlement.amount <= Decimal("0.00"):
        raise _error(
            "payload_invalid",
            "Successful payment webhook amount must be positive",
        )
    if (
        settlement.provider_fee < Decimal("0.00")
        or settlement.provider_fee > settlement.amount
    ):
        raise _error(
            "payload_invalid",
            "Payment webhook provider fee is outside the confirmed amount",
        )
    if not external_id:
        raise _error(
            "payload_invalid",
            "Successful payment webhook omitted its provider transaction identity",
        )
    invoice_id = _metadata_uuid(
        settlement.metadata,
        field="invoice_id",
        error_suffix="payload_invalid",
    )
    return _PreparedPaymentWebhook(
        ingest=replace(
            ingest,
            amount=settlement.amount,
            provider_fee=settlement.provider_fee,
            net_amount=(
                round_money(topup_intent.requested_amount)
                if topup_intent is not None
                else round_money(settlement.amount - settlement.provider_fee)
            ),
            provider_reference=settlement.reference,
            topup_intent_id=(topup_intent.id if topup_intent is not None else None),
            currency=settlement.currency,
            invoice_id=invoice_id,
            account_id=(topup_intent.account_id if topup_intent is not None else None),
            billing_account_id=(
                topup_intent.billing_account_id if topup_intent is not None else None
            ),
            observed_payment_status=settlement.status,
        ),
        settlement=settlement,
        topup_intent=topup_intent,
    )


def _stage_deposit_settlement(
    db: Session,
    *,
    prepared: _PreparedPaymentWebhook,
    provider: PaymentWebhookProvider,
    context: CommandContext,
) -> _PreparedPaymentWebhook:
    settlement = prepared.settlement
    intent = prepared.topup_intent
    if (
        settlement is None
        or settlement.status != PaymentStatus.succeeded
        or intent is None
        or intent.purpose != "account_credit_deposit"
    ):
        return prepared
    external_id = prepared.ingest.external_id
    if settlement.amount is None or not external_id:
        raise _error(
            "deposit_rejected",
            "Deposit provider confirmation omitted amount or transaction identity",
        )
    provider_intent_id = _optional_uuid(settlement.metadata.get("topup_intent_id"))
    if provider_intent_id is None:
        raise _error(
            "deposit_rejected",
            "Deposit provider confirmation omitted intent correlation",
            intent_id=str(intent.id),
        )
    try:
        result = AccountCreditDeposits.stage_verified_settlement(
            db,
            SettleAccountCreditDepositCommand(
                intent_id=intent.id,
                provider_type=provider.value,
                external_transaction_id=external_id,
                amount=settlement.amount,
                currency=settlement.currency or intent.currency,
                provider_intent_id=provider_intent_id,
                source=AccountCreditDepositSettlementSource.provider_webhook,
                provider_fee=settlement.provider_fee,
            ),
            context=CommandContext.system(
                actor=context.actor,
                scope=SETTLEMENT_PARTICIPANT_SCOPE,
                reason="Stage webhook-confirmed account-credit deposit",
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
                idempotency_key=f"account-credit-deposit-{intent.id}",
            ),
        )
    except DepositEligibilityError as exc:
        raise _error(
            "deposit_rejected",
            str(exc),
            deposit_error_code=exc.code,
            intent_id=str(intent.id),
        ) from exc
    return replace(
        prepared,
        ingest=replace(prepared.ingest, payment_id=result.payment.id),
    )


def _stage_provider_event(
    db: Session,
    ingest: PaymentProviderEventCommand,
    *,
    context: CommandContext,
) -> PaymentProviderEventResult:
    try:
        return billing_service.payment_provider_events.stage_verified_webhook_event(
            db,
            ingest,
            context=CommandContext.system(
                actor=context.actor,
                scope=WEBHOOK_PARTICIPANT_SCOPE,
                reason="Stage signature-verified payment-provider observation",
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
                idempotency_key=ingest.idempotency_key,
            ),
        )
    except PaymentProviderEventError as exc:
        raise _error(
            "provider_event_rejected",
            exc.message,
            provider_event_error_code=exc.code,
        ) from exc


def _stage_topup_consequences(
    db: Session,
    *,
    prepared: _PreparedPaymentWebhook,
    event: PaymentProviderEventResult,
    context: CommandContext,
) -> None:
    settlement = prepared.settlement
    intent = prepared.topup_intent
    if (
        settlement is None
        or settlement.status != PaymentStatus.succeeded
        or event.payment_id is None
        or intent is None
        or intent.purpose == "account_credit_deposit"
    ):
        return
    try:
        stage_topup_intent_completion(
            db,
            CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=event.payment_id,
                source=TopupIntentCompletionSource.provider_webhook,
            ),
            context=CommandContext.system(
                actor=context.actor,
                scope=COMPLETION_SCOPE,
                reason="Project webhook payment onto top-up intent",
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
            ),
        )
    except TopupIntentError as exc:
        raise _error(
            "topup_projection_rejected",
            exc.message,
            topup_error_code=exc.code,
            intent_id=str(intent.id),
        ) from exc


def _result_from_consequence(
    receipt: IntegrationInbox,
    provider: PaymentWebhookProvider,
) -> ProcessedPaymentWebhook:
    consequence = receipt.consequence_json or {}
    return ProcessedPaymentWebhook(
        receipt_id=receipt.id,
        provider=provider,
        provider_event_id=_optional_uuid(consequence.get("provider_event_id")),
        payment_id=_optional_uuid(consequence.get("payment_id")),
        replayed=True,
    )


def process_claimed_payment_webhook(
    db: Session,
    command: ProcessClaimedPaymentWebhookCommand,
    *,
    context: CommandContext,
) -> ProcessedPaymentWebhook:
    """Commit one complete billing consequence for a claimed verified receipt."""

    return execute_owner_command(
        db,
        definition=_PROCESS_COMMAND,
        context=context,
        operation=lambda: _process_claimed_payment_webhook(
            db,
            command=command,
            context=context,
        ),
    )


def _process_claimed_payment_webhook(
    db: Session,
    *,
    command: ProcessClaimedPaymentWebhookCommand,
    context: CommandContext,
) -> ProcessedPaymentWebhook:
    receipt = lock_for_update(db, IntegrationInbox, command.receipt_id)
    if receipt is None:
        raise _error(
            "receipt_not_found",
            "Claimed payment webhook receipt was not found",
            receipt_id=str(command.receipt_id),
        )
    recorded_provider = str((receipt.headers_json or {}).get("provider") or "")
    if recorded_provider != command.provider.value:
        raise _error(
            "receipt_provider_mismatch",
            "Payment webhook provider does not match the verified receipt",
            receipt_id=str(receipt.id),
        )
    if receipt.state == "processed":
        return _result_from_consequence(receipt, command.provider)
    if receipt.state != "processing":
        raise _error(
            "receipt_not_claimed",
            "Payment webhook receipt must be claimed before processing",
            receipt_id=str(receipt.id),
            state=receipt.state,
        )
    provider = billing_service.payment_providers.get_by_type(
        db,
        PaymentProviderType(command.provider.value),
    )
    if provider is None:
        raise _error(
            "provider_not_configured",
            "No payment provider is configured for this verified receipt",
            provider=command.provider.value,
        )
    try:
        prepared = _prepare_payment_webhook(
            db,
            receipt=receipt,
            provider=command.provider,
            provider_id=provider.id,
        )
    except PaymentWebhookError:
        raise
    except ValueError as exc:
        raise _error(
            "payload_invalid",
            "Payment webhook could not be normalized",
        ) from exc
    prepared = _stage_deposit_settlement(
        db,
        prepared=prepared,
        provider=command.provider,
        context=context,
    )
    event = _stage_provider_event(db, prepared.ingest, context=context)
    if (
        prepared.settlement is not None
        and prepared.settlement.status == PaymentStatus.succeeded
        and event.payment_id is None
    ):
        raise _error(
            "settlement_unlinked",
            "Successful settlement did not post or link a payment",
            provider_event_id=str(event.id),
        )
    _stage_topup_consequences(
        db,
        prepared=prepared,
        event=event,
        context=context,
    )
    result = ProcessedPaymentWebhook(
        receipt_id=receipt.id,
        provider=command.provider,
        provider_event_id=event.id,
        payment_id=event.payment_id,
    )
    integration_inbox.mark_processed(receipt, consequence=result.consequence())
    db.flush()
    return result


__all__ = [
    "PROCESS_SCOPE",
    "PaymentWebhookError",
    "PaymentWebhookProvider",
    "PaymentWebhookReceiptIdentity",
    "ProcessClaimedPaymentWebhookCommand",
    "ProcessedPaymentWebhook",
    "identify_verified_payment_webhook",
    "process_claimed_payment_webhook",
]
