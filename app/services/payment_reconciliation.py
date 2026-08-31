"""Reconcile stranded gateway top-ups from authoritative observations.

The sweep selects immutable candidates and asks the payment transport for one
fact at a time. Each resulting billing consequence is then committed by one
manifest-verified owner command using the existing deposit, provider-event,
and top-up intent participants. Gateway calls never run inside that business
transaction and every intent remains an independent retry boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.audit import AuditActorType
from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.domain_settings import SettingDomain
from app.models.paystack_recovery import PaystackOutsideWindowRecoveryRun
from app.services import settings_spec
from app.services.account_credit_deposits import (
    PURPOSE as ACCOUNT_CREDIT_DEPOSIT_PURPOSE,
)
from app.services.account_credit_deposits import (
    SETTLEMENT_PARTICIPANT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    DepositEligibilityError,
    SettleAccountCreditDepositCommand,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.common import round_money, to_decimal
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.payment_gateway_adapter import (
    PaymentGatewayTransaction,
    PaymentGatewayVerificationObservation,
    PaymentGatewayVerificationOutcome,
    payment_gateway_adapter,
)
from app.services.payment_gateway_finance import (
    PaymentGatewayFinanceError,
    require_active_gateway_provider,
)
from app.services.payment_provider_events import (
    RECONCILIATION_PARTICIPANT_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEvents,
)
from app.services.payment_routing import (
    SUPPORTED_PROVIDER_TYPES,
    parse_supported_provider_type,
)
from app.services.topup_intents import (
    COMPLETION_SCOPE,
    CompleteTopupIntentCommand,
    GatewayTopupIntentFlow,
    GatewayTopupObservationSource,
    RecordGatewayTopupObservationCommand,
    RecordGatewayTopupReconciliationAttemptCommand,
    TopupIntentCompletionSource,
    TopupIntentError,
    TopupIntentReconciliationAttemptResult,
    TopupIntentStatus,
    lock_topup_intent_scope,
    stage_gateway_topup_observation,
    stage_gateway_topup_reconciliation_attempt,
    stage_topup_intent_completion,
)
from app.services.topup_intents import (
    GATEWAY_OBSERVATION_SCOPE as INTENT_OBSERVATION_SCOPE,
)

logger = logging.getLogger(__name__)

RECONCILIATION_SCOPE = "topup-payment:reconcile"
VERIFIED_SETTLEMENT_SCOPE = "topup-payment:reconcile-verified"
GATEWAY_OBSERVATION_SCOPE = "topup-payment:reconcile-observation"
RECONCILIATION_ATTEMPT_SCOPE = "topup-payment:reconcile-attempt"
PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE = "topup-payment:outside-window-recovery"
_TERMINAL_RECOVERY_STATUSES = (
    TopupIntentStatus.failed,
    TopupIntentStatus.abandoned,
    TopupIntentStatus.canceled,
    TopupIntentStatus.expired,
)
_TERMINAL_LANE_PERCENT = 20

_VERIFIED_SETTLEMENT_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_reconciliation",
    concern="verified provider settlement then allocation orchestration",
    name="settle_verified_reconciled_topup",
)
_GATEWAY_OBSERVATION_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_reconciliation",
    concern="stranded top-up reconciliation",
    name="record_reconciled_gateway_observation",
)
_RECONCILIATION_ATTEMPT_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_reconciliation",
    concern="stranded top-up reconciliation",
    name="claim_topup_reconciliation_attempt",
)
_PAYSTACK_OUTSIDE_WINDOW_RECOVERY_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_reconciliation",
    concern="finance-reviewed exact-reference outside-window top-up recovery",
    name="confirm_paystack_outside_window_recovery",
)


class PaymentReconciliationError(DomainError, ValueError):
    """Stable transport-neutral rejection from payment reconciliation."""


class TopupReconciliationDisposition(str, Enum):
    recovered = "recovered"
    linked = "linked"
    expired = "expired"
    failed = "failed"
    abandoned = "abandoned"
    unchanged = "unchanged"


class PaystackOutsideWindowRecoveryDisposition(str, Enum):
    recoverable = "recoverable"
    reference_not_found = "reference_not_found"
    awaiting_confirmation = "awaiting_confirmation"
    processing = "processing"
    provider_failed = "provider_failed"
    provider_abandoned = "provider_abandoned"
    provider_unavailable = "provider_unavailable"
    evidence_incomplete = "evidence_incomplete"
    conflict = "conflict"


@dataclass(frozen=True, slots=True)
class RunTopupReconciliationCommand:
    """Canonical schedule time for one bounded reconciliation sweep."""

    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TopupReconciliationCandidate:
    """Immutable identity passed across the external observation boundary."""

    intent_id: UUID
    provider_type: PaymentProviderType
    reference: str
    status: TopupIntentStatus = TopupIntentStatus.pending


@dataclass(frozen=True, slots=True)
class ReconcileVerifiedTopupCommand:
    candidate: TopupReconciliationCandidate
    transaction: PaymentGatewayTransaction
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileGatewayObservationCommand:
    candidate: TopupReconciliationCandidate
    observation: PaymentGatewayVerificationObservation
    observed_at: datetime
    source: GatewayTopupObservationSource


@dataclass(frozen=True, slots=True)
class ReconciledTopupResult:
    intent_id: UUID
    disposition: TopupReconciliationDisposition
    payment_id: UUID | None = None
    provider_event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PreviewPaystackOutsideWindowRecoveryQuery:
    intent_id: UUID
    reference: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PaystackOutsideWindowRecoveryPreview:
    intent_id: UUID
    reference: str
    disposition: PaystackOutsideWindowRecoveryDisposition
    actionable: bool
    fingerprint: str
    intent_status: TopupIntentStatus
    intent_created_at: datetime
    provider_id: UUID
    checkout_binding_id: UUID | None
    provider_external_id: str | None
    gross_amount: Decimal | None
    provider_fee: Decimal | None
    authorized_net_amount: Decimal | None
    currency: str | None
    provider_status: str | None
    reason_code: str
    existing_payment_id: UUID | None


@dataclass(frozen=True, slots=True)
class ConfirmPaystackOutsideWindowRecoveryCommand:
    intent_id: UUID
    reference: str
    preview_fingerprint: str
    review_reference: str
    confirmed: bool
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PaystackOutsideWindowRecoveryResult:
    recovery_run_id: UUID
    intent_id: UUID
    disposition: TopupReconciliationDisposition
    payment_id: UUID
    preview_fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class TopupReconciliationSummary:
    selected: int = 0
    checked: int = 0
    checked_pending: int = 0
    checked_terminal: int = 0
    recovered: int = 0
    linked: int = 0
    expired: int = 0
    failed: int = 0
    abandoned: int = 0
    unchanged: int = 0
    errors: int = 0
    pending_due_remaining: int = 0
    terminal_due_remaining: int = 0
    outside_window: int = 0
    saturated: bool = False
    partial: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        """Serialize the typed result at the Celery transport boundary."""

        return {
            "selected": self.selected,
            "checked": self.checked,
            "checked_pending": self.checked_pending,
            "checked_terminal": self.checked_terminal,
            "recovered": self.recovered,
            "linked": self.linked,
            "expired": self.expired,
            "failed": self.failed,
            "abandoned": self.abandoned,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "pending_due_remaining": self.pending_due_remaining,
            "terminal_due_remaining": self.terminal_due_remaining,
            "outside_window": self.outside_window,
            "saturated": self.saturated,
            "partial": self.partial,
        }


@dataclass(frozen=True, slots=True)
class TopupReconciliationBacklog:
    """Read-only projection of gateway intents against reconciliation policy."""

    pending_total: int
    pending_fresh: int
    pending_due: int
    pending_cooling_down: int
    pending_outside_window: int
    terminal_recovery_total: int
    terminal_recovery_due: int
    terminal_recovery_cooling_down: int
    terminal_recovery_outside_window: int
    oldest_pending_at: datetime | None
    oldest_pending_due_created_at: datetime | None
    oldest_terminal_due_created_at: datetime | None
    stale_before: datetime
    oldest_eligible_at: datetime

    @property
    def pending(self) -> int:
        """Compatibility alias for the complete pending population."""

        return self.pending_total

    @property
    def pending_eligible(self) -> int:
        """Compatibility alias for pending work due now."""

        return self.pending_due

    @property
    def eligible(self) -> int:
        """All automatic reconciliation work due now."""

        return self.pending_due + self.terminal_recovery_due

    @property
    def outside_window(self) -> int:
        """All unresolved work older than the automatic retry window."""

        return self.pending_outside_window + self.terminal_recovery_outside_window

    @property
    def oldest_due_terminal_at(self) -> datetime | None:
        """Compatibility alias with its historical name."""

        return self.oldest_terminal_due_created_at


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> PaymentReconciliationError:
    return PaymentReconciliationError(
        code=f"financial.payment_reconciliation.{suffix}",
        message=message,
        details=details,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _resolve_reconciliation_int_setting(db: Session, key: str) -> int:
    spec = settings_spec.get_spec(SettingDomain.billing, key)
    if spec is None or not isinstance(spec.default, int):
        raise _error(
            "policy_missing",
            "Top-up reconciliation policy is not registered",
            setting=key,
        )
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = spec.default
    if spec.min_value is not None:
        parsed = max(int(spec.min_value), parsed)
    if spec.max_value is not None:
        parsed = min(int(spec.max_value), parsed)
    return parsed


def _gateway_reconcile_due(observed_at: datetime) -> ColumnElement[bool]:
    return (TopupIntent.gateway_next_reconcile_at.is_(None)) | (
        TopupIntent.gateway_next_reconcile_at <= observed_at
    )


def _next_gateway_reconcile_at(
    db: Session,
    *,
    intent: TopupIntent,
    observation: PaymentGatewayVerificationObservation,
    observed_at: datetime,
) -> datetime:
    terminal_retry_hours = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_terminal_retry_hours",
    )
    pending_retry_minutes = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_pending_retry_minutes",
    )
    processing_retry_minutes = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_processing_retry_minutes",
    )
    unavailable_retry_minutes = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_unavailable_retry_minutes",
    )

    status = TopupIntentStatus(intent.status)
    expires_at = _as_utc(intent.expires_at) if intent.expires_at is not None else None
    if (
        status in _TERMINAL_RECOVERY_STATUSES
        or observation.outcome
        in {
            PaymentGatewayVerificationOutcome.failed,
            PaymentGatewayVerificationOutcome.abandoned,
        }
        or (
            status is TopupIntentStatus.pending
            and expires_at is not None
            and observed_at >= expires_at
        )
    ):
        return observed_at + timedelta(hours=terminal_retry_hours)
    if observation.outcome is PaymentGatewayVerificationOutcome.processing:
        return observed_at + timedelta(minutes=processing_retry_minutes)
    if observation.outcome in {
        PaymentGatewayVerificationOutcome.unavailable,
        PaymentGatewayVerificationOutcome.unknown,
    }:
        return observed_at + timedelta(minutes=unavailable_retry_minutes)
    return observed_at + timedelta(minutes=pending_retry_minutes)


def _attempt_retry_at(
    db: Session,
    *,
    candidate: TopupReconciliationCandidate,
    attempted_at: datetime,
) -> datetime:
    """Lease one selected row long enough for this run to finish safely."""

    if candidate.status in _TERMINAL_RECOVERY_STATUSES:
        retry_hours = _resolve_reconciliation_int_setting(
            db,
            "topup_reconciliation_terminal_retry_hours",
        )
        return attempted_at + timedelta(hours=retry_hours)
    retry_minutes = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_pending_retry_minutes",
    )
    return attempted_at + timedelta(minutes=retry_minutes)


def _target_invoice_id(intent: TopupIntent) -> UUID | None:
    """Resolve an explicit invoice instruction; never guess a replacement."""

    metadata = intent.metadata_ or {}
    if (
        str(metadata.get("payment_flow"))
        != GatewayTopupIntentFlow.invoice_payment.value
    ):
        return None
    raw_invoice_id = metadata.get("invoice_id")
    try:
        return UUID(str(raw_invoice_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _error(
            "invoice_correlation_invalid",
            "Invoice-payment intent has no valid target invoice",
            intent_id=str(intent.id),
        ) from exc


def _validate_candidate(
    intent: TopupIntent,
    candidate: TopupReconciliationCandidate,
) -> None:
    try:
        intent_provider = parse_supported_provider_type(intent.provider_type)
    except ValueError as exc:
        raise _error(
            "provider_mismatch",
            "Top-up intent provider is not eligible for gateway reconciliation",
            intent_id=str(intent.id),
        ) from exc
    if intent_provider is not candidate.provider_type:
        raise _error(
            "provider_mismatch",
            "Reconciliation candidate provider does not match the top-up intent",
            intent_id=str(intent.id),
        )
    if intent.reference != candidate.reference:
        raise _error(
            "reference_mismatch",
            "Reconciliation candidate reference does not match the top-up intent",
            intent_id=str(intent.id),
        )


def _normalized_transaction(
    command: ReconcileVerifiedTopupCommand,
) -> tuple[str, Decimal, Decimal, str]:
    transaction = command.transaction
    if transaction.provider_type != command.candidate.provider_type.value:
        raise _error(
            "provider_mismatch",
            "Gateway observation provider does not match the selected candidate",
            intent_id=str(command.candidate.intent_id),
        )
    external_id = transaction.external_id.strip()
    if not external_id or len(external_id) > 120:
        raise _error(
            "transaction_identity_invalid",
            "Gateway observation omitted a valid transaction identity",
            intent_id=str(command.candidate.intent_id),
        )
    try:
        amount = round_money(to_decimal(transaction.amount))
        provider_fee = round_money(to_decimal(transaction.provider_fee))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "amount_invalid",
            "Gateway observation contains invalid monetary evidence",
            intent_id=str(command.candidate.intent_id),
        ) from exc
    if amount <= Decimal("0.00"):
        raise _error(
            "amount_invalid",
            "Gateway observation amount must be positive",
            intent_id=str(command.candidate.intent_id),
        )
    if provider_fee < Decimal("0.00") or provider_fee > amount:
        raise _error(
            "provider_fee_invalid",
            "Gateway observation fee must be between zero and the gross amount",
            intent_id=str(command.candidate.intent_id),
        )
    currency = transaction.currency.strip().upper()
    if len(currency) != 3:
        raise _error(
            "currency_invalid",
            "Gateway observation currency must be a three-letter code",
            intent_id=str(command.candidate.intent_id),
        )
    reflected_reference = str(
        transaction.raw.get(
            "reference" if transaction.provider_type == "paystack" else "tx_ref"
        )
        or ""
    ).strip()
    if reflected_reference != command.candidate.reference:
        raise _error(
            "provider_reference_mismatch",
            "Gateway observation did not echo the selected reference",
            intent_id=str(command.candidate.intent_id),
        )
    return external_id, amount, provider_fee, currency


def _active_provider_for_intent(db: Session, intent: TopupIntent) -> PaymentProvider:
    try:
        return require_active_gateway_provider(
            db,
            provider_type=PaymentProviderType(intent.provider_type),
            expected_provider_id=intent.provider_id,
        )
    except PaymentGatewayFinanceError as exc:
        if exc.code.endswith("provider_identity_ambiguous"):
            raise _error(
                "provider_configuration_ambiguous",
                "Top-up intent provider configuration is ambiguous",
                intent_id=str(intent.id),
            ) from exc
        raise _error(
            "provider_configuration_mismatch",
            "Top-up intent provider configuration is missing, disabled, or mismatched",
            intent_id=str(intent.id),
        ) from exc


def _authorized_settlement_amount(
    *,
    intent: TopupIntent,
    gross_amount: Decimal,
    provider_fee: Decimal,
) -> Decimal:
    """Apply the owning domain's existing fee policy without inventing a global one."""

    requested_amount = round_money(intent.requested_amount)
    if intent.purpose == ACCOUNT_CREDIT_DEPOSIT_PURPOSE:
        authorized_amount = round_money(gross_amount - provider_fee)
        if authorized_amount != requested_amount:
            raise _error(
                "authorized_net_mismatch",
                "Gateway net amount does not match the authorized account deposit",
                intent_id=str(intent.id),
            )
        return authorized_amount
    if requested_amount <= Decimal("0.00") or requested_amount > gross_amount:
        raise _error(
            "authorized_net_mismatch",
            "Authorized settlement amount is invalid for the observed gateway gross",
            intent_id=str(intent.id),
        )
    return requested_amount


def _payment_matches_verified_evidence(
    payment: Payment,
    *,
    intent: TopupIntent,
    provider_id: UUID,
    external_id: str,
    gross_amount: Decimal,
    provider_fee: Decimal,
    authorized_net_amount: Decimal,
    currency: str,
) -> bool:
    settlement = payment.settlement
    return all(
        (
            payment.is_active,
            payment.status is PaymentStatus.succeeded,
            payment.provider_id == provider_id,
            payment.account_id == intent.account_id,
            payment.billing_account_id == intent.billing_account_id,
            str(payment.external_id or "").strip() == external_id,
            round_money(payment.amount) == gross_amount,
            round_money(payment.provider_fee) == provider_fee,
            payment.currency.strip().upper() == currency,
            settlement is not None,
            settlement is not None
            and round_money(settlement.amount) == authorized_net_amount,
        )
    )


def _participant_context(
    context: CommandContext,
    *,
    scope: str,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    return CommandContext.system(
        actor=context.actor,
        scope=scope,
        reason=reason,
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
        idempotency_key=idempotency_key,
    )


def _stage_verified_settlement_for_intent(
    db: Session,
    command: ReconcileVerifiedTopupCommand,
    *,
    context: CommandContext,
    intent: TopupIntent,
    invoice_id: UUID | None,
) -> ReconciledTopupResult:
    _validate_candidate(intent, command.candidate)
    provider = _active_provider_for_intent(db, intent)
    external_id, amount, provider_fee, currency = _normalized_transaction(command)
    if currency != intent.currency.strip().upper():
        raise _error(
            "currency_mismatch",
            "Gateway observation currency does not match the top-up intent",
            intent_id=str(intent.id),
        )
    authorized_net_amount = _authorized_settlement_amount(
        intent=intent,
        gross_amount=amount,
        provider_fee=provider_fee,
    )

    if intent.completed_payment_id is not None:
        payment = db.get(Payment, intent.completed_payment_id)
        if payment is None or not _payment_matches_verified_evidence(
            payment,
            intent=intent,
            provider_id=provider.id,
            external_id=external_id,
            gross_amount=amount,
            provider_fee=provider_fee,
            authorized_net_amount=authorized_net_amount,
            currency=currency,
        ):
            raise _error(
                "completion_conflict",
                "Completed top-up intent has different payment evidence",
                intent_id=str(intent.id),
            )
        return ReconciledTopupResult(
            intent_id=intent.id,
            payment_id=payment.id,
            disposition=TopupReconciliationDisposition.linked,
        )

    event_key = f"{command.candidate.provider_type.value}-{intent.reference}"
    existing_payment_id = db.scalar(
        select(Payment.id)
        .where(Payment.provider_id == provider.id)
        .where(Payment.external_id == external_id)
        .where(Payment.is_active.is_(True))
        .order_by(Payment.created_at.asc())
    )
    deposit_replay = False
    payment_id: UUID | None = None
    if intent.purpose == ACCOUNT_CREDIT_DEPOSIT_PURPOSE:
        try:
            deposit = AccountCreditDeposits.stage_verified_settlement(
                db,
                SettleAccountCreditDepositCommand(
                    intent_id=intent.id,
                    provider_type=command.candidate.provider_type.value,
                    external_transaction_id=external_id,
                    amount=amount,
                    currency=currency,
                    provider_intent_id=intent.id,
                    source=(
                        AccountCreditDepositSettlementSource.gateway_reconciliation
                    ),
                    provider_fee=provider_fee,
                ),
                context=_participant_context(
                    context,
                    scope=SETTLEMENT_PARTICIPANT_SCOPE,
                    reason="Stage gateway-reconciled account-credit deposit",
                    idempotency_key=f"account-credit-deposit-{intent.id}",
                ),
            )
        except DepositEligibilityError as exc:
            raise _error(
                "deposit_rejected",
                str(exc),
                intent_id=str(intent.id),
                deposit_error_code=exc.code,
            ) from exc
        payment_id = deposit.payment.id
        deposit_replay = deposit.already_recorded

    ingest = PaymentProviderEventCommand(
        provider_id=provider.id,
        payment_id=payment_id,
        invoice_id=invoice_id,
        account_id=intent.account_id,
        billing_account_id=intent.billing_account_id,
        event_type="gateway.reconciliation.succeeded",
        external_id=external_id,
        idempotency_key=event_key,
        amount=amount,
        provider_fee=provider_fee,
        net_amount=authorized_net_amount,
        provider_reference=intent.reference,
        topup_intent_id=intent.id,
        currency=currency,
        payload={
            "source": "gateway_reconciliation",
            "provider_type": command.candidate.provider_type.value,
            "reference": intent.reference,
            "external_id": external_id,
            "observed_at": _as_utc(command.observed_at).isoformat(),
        },
        observed_payment_status=PaymentStatus.succeeded,
    )
    try:
        event = PaymentProviderEvents.stage_verified_reconciliation_event(
            db,
            ingest,
            context=_participant_context(
                context,
                scope=RECONCILIATION_PARTICIPANT_SCOPE,
                reason="Stage gateway-verified payment-provider observation",
                idempotency_key=event_key,
            ),
        )
    except PaymentProviderEventError as exc:
        raise _error(
            "provider_event_rejected",
            exc.message,
            intent_id=str(intent.id),
            provider_event_error_code=exc.code,
        ) from exc
    if event.payment_id is None:
        raise _error(
            "settlement_unlinked",
            "Successful gateway observation did not post or link a payment",
            intent_id=str(intent.id),
        )
    try:
        stage_topup_intent_completion(
            db,
            CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=event.payment_id,
                source=TopupIntentCompletionSource.gateway_reconciliation,
            ),
            context=_participant_context(
                context,
                scope=COMPLETION_SCOPE,
                reason="Project reconciled payment onto the top-up intent",
                idempotency_key=f"topup-completion-{intent.id}",
            ),
        )
    except TopupIntentError as exc:
        raise _error(
            "topup_projection_rejected",
            exc.message,
            intent_id=str(intent.id),
            topup_error_code=exc.code,
        ) from exc
    replayed = (
        deposit_replay if payment_id is not None else existing_payment_id is not None
    )
    return ReconciledTopupResult(
        intent_id=intent.id,
        payment_id=event.payment_id,
        provider_event_id=event.id,
        disposition=(
            TopupReconciliationDisposition.linked
            if replayed
            else TopupReconciliationDisposition.recovered
        ),
    )


def _stage_verified_settlement(
    db: Session,
    command: ReconcileVerifiedTopupCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    """Preserve the scheduled legacy invoice instruction at its existing boundary."""

    intent = lock_topup_intent_scope(db, command.candidate.intent_id)
    return _stage_verified_settlement_for_intent(
        db,
        command,
        context=context,
        intent=intent,
        invoice_id=_target_invoice_id(intent),
    )


def settle_verified_reconciled_topup(
    db: Session,
    command: ReconcileVerifiedTopupCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    """Commit one verified provider consequence as an independent root."""

    return execute_owner_command(
        db,
        definition=_VERIFIED_SETTLEMENT_COMMAND,
        context=context,
        operation=lambda: _stage_verified_settlement(
            db,
            command,
            context=context,
        ),
    )


def _validate_outside_window_intent(
    db: Session,
    *,
    query: PreviewPaystackOutsideWindowRecoveryQuery,
    intent: TopupIntent,
) -> tuple[TopupIntentStatus, int]:
    reference = query.reference.strip()
    if not reference or len(reference) > 120 or reference != query.reference:
        raise _error(
            "recovery_reference_invalid",
            "An exact, non-empty Paystack reference is required",
        )
    if intent.reference != reference:
        raise _error(
            "recovery_reference_mismatch",
            "The reference does not belong to the selected top-up intent",
            intent_id=str(query.intent_id),
        )
    if intent.provider_type != PaymentProviderType.paystack.value:
        raise _error(
            "recovery_provider_invalid",
            "Reviewed outside-window recovery is Paystack-only",
            intent_id=str(query.intent_id),
        )
    try:
        status = TopupIntentStatus(intent.status)
    except ValueError as exc:
        raise _error(
            "recovery_status_invalid",
            "Top-up intent has an unsupported recovery status",
            intent_id=str(query.intent_id),
        ) from exc
    if status not in _TERMINAL_RECOVERY_STATUSES:
        raise _error(
            "recovery_status_ineligible",
            "Only a terminal top-up intent can use reviewed outside-window recovery",
            intent_id=str(query.intent_id),
            status=status.value,
        )
    if intent.completed_payment_id is not None:
        raise _error(
            "recovery_already_completed",
            "The top-up intent is already linked to a completed payment",
            intent_id=str(query.intent_id),
        )
    if (intent.account_id is None) == (intent.billing_account_id is None):
        raise _error(
            "recovery_scope_invalid",
            "Top-up intent must identify exactly one customer billing scope",
            intent_id=str(query.intent_id),
        )
    legacy_invoice_id = _target_invoice_id(intent)
    if legacy_invoice_id is not None and legacy_invoice_id != intent.invoice_id:
        raise _error(
            "recovery_structural_invoice_required",
            "Reviewed recovery refuses a metadata-only or mismatched invoice instruction",
            intent_id=str(query.intent_id),
        )
    max_age_days = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_max_age_days",
    )
    oldest_eligible_at = _as_utc(query.observed_at) - timedelta(days=max_age_days)
    if _as_utc(intent.created_at) > oldest_eligible_at:
        raise _error(
            "recovery_inside_automatic_window",
            "Top-up intent remains inside the automatic reconciliation window",
            intent_id=str(query.intent_id),
        )
    return status, max_age_days


def _load_outside_window_intent(
    db: Session,
    query: PreviewPaystackOutsideWindowRecoveryQuery,
) -> tuple[TopupIntent, TopupIntentStatus, int, PaymentProvider]:
    intent = db.get(TopupIntent, query.intent_id)
    if intent is None:
        raise _error(
            "recovery_intent_not_found",
            "Top-up intent was not found",
            intent_id=str(query.intent_id),
        )
    status, max_age_days = _validate_outside_window_intent(
        db,
        query=query,
        intent=intent,
    )
    provider = _active_provider_for_intent(db, intent)
    return intent, status, max_age_days, provider


def _existing_payment_material(payment: Payment | None) -> dict[str, object] | None:
    if payment is None:
        return None
    settlement = payment.settlement
    return {
        "id": str(payment.id),
        "provider_id": str(payment.provider_id) if payment.provider_id else None,
        "account_id": str(payment.account_id) if payment.account_id else None,
        "billing_account_id": (
            str(payment.billing_account_id) if payment.billing_account_id else None
        ),
        "external_id": str(payment.external_id or ""),
        "status": payment.status.value,
        "active": bool(payment.is_active),
        "gross_amount": f"{round_money(payment.amount):.2f}",
        "provider_fee": f"{round_money(payment.provider_fee):.2f}",
        "authorized_net_amount": (
            f"{round_money(settlement.amount):.2f}" if settlement is not None else None
        ),
        "currency": payment.currency.strip().upper(),
    }


def _existing_event_material(
    event: PaymentProviderEvent | None,
) -> dict[str, object] | None:
    if event is None:
        return None
    return {
        "id": str(event.id),
        "payment_id": str(event.payment_id) if event.payment_id else None,
        "external_id": str(event.external_id or ""),
        "provider_reference": str(event.provider_reference or ""),
        "observed_payment_status": (
            event.observed_payment_status.value
            if event.observed_payment_status is not None
            else None
        ),
        "amount": f"{round_money(event.amount):.2f}"
        if event.amount is not None
        else None,
        "provider_fee": f"{round_money(event.provider_fee):.2f}",
        "net_amount": (
            f"{round_money(event.net_amount):.2f}"
            if event.net_amount is not None
            else None
        ),
        "currency": str(event.currency or "").strip().upper() or None,
        "observation_digest": event.observation_digest,
        "source": event.source.value,
        "financial_effect": event.financial_effect.value,
        "status": event.status.value,
        "error_code": event.error_code,
        "processed_at": (
            _as_utc(event.processed_at).isoformat()
            if event.processed_at is not None
            else None
        ),
    }


def _recovery_fingerprint(material: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _preview_from_observation(
    db: Session,
    *,
    query: PreviewPaystackOutsideWindowRecoveryQuery,
    intent: TopupIntent,
    status: TopupIntentStatus,
    max_age_days: int,
    provider: PaymentProvider,
    observation: PaymentGatewayVerificationObservation,
) -> PaystackOutsideWindowRecoveryPreview:
    disposition = {
        PaymentGatewayVerificationOutcome.awaiting_confirmation: (
            PaystackOutsideWindowRecoveryDisposition.awaiting_confirmation
        ),
        PaymentGatewayVerificationOutcome.processing: (
            PaystackOutsideWindowRecoveryDisposition.processing
        ),
        PaymentGatewayVerificationOutcome.failed: (
            PaystackOutsideWindowRecoveryDisposition.provider_failed
        ),
        PaymentGatewayVerificationOutcome.abandoned: (
            PaystackOutsideWindowRecoveryDisposition.provider_abandoned
        ),
        PaymentGatewayVerificationOutcome.unavailable: (
            PaystackOutsideWindowRecoveryDisposition.provider_unavailable
        ),
        PaymentGatewayVerificationOutcome.unknown: (
            PaystackOutsideWindowRecoveryDisposition.evidence_incomplete
        ),
    }.get(
        observation.outcome,
        PaystackOutsideWindowRecoveryDisposition.recoverable,
    )
    if observation.reason_code.value == "provider_reference_not_found":
        disposition = PaystackOutsideWindowRecoveryDisposition.reference_not_found

    external_id: str | None = None
    gross_amount: Decimal | None = None
    provider_fee: Decimal | None = None
    authorized_net_amount: Decimal | None = None
    currency: str | None = None
    existing_payment: Payment | None = None
    existing_event: PaymentProviderEvent | None = None
    evidence_error: str | None = None
    if observation.outcome is PaymentGatewayVerificationOutcome.succeeded:
        if observation.transaction is None:
            disposition = PaystackOutsideWindowRecoveryDisposition.evidence_incomplete
            evidence_error = "observation_incomplete"
        else:
            settlement_command = ReconcileVerifiedTopupCommand(
                candidate=TopupReconciliationCandidate(
                    intent_id=intent.id,
                    provider_type=PaymentProviderType.paystack,
                    reference=intent.reference,
                    status=status,
                ),
                transaction=observation.transaction,
                observed_at=query.observed_at,
            )
            try:
                external_id, gross_amount, provider_fee, currency = (
                    _normalized_transaction(settlement_command)
                )
                authorized_net_amount = _authorized_settlement_amount(
                    intent=intent,
                    gross_amount=gross_amount,
                    provider_fee=provider_fee,
                )
                if currency != intent.currency.strip().upper():
                    raise _error(
                        "currency_mismatch",
                        "Gateway currency does not match the selected intent",
                    )
            except PaymentReconciliationError as exc:
                disposition = PaystackOutsideWindowRecoveryDisposition.conflict
                evidence_error = exc.code

    if all(
        value is not None
        for value in (
            external_id,
            gross_amount,
            provider_fee,
            authorized_net_amount,
            currency,
        )
    ):
        assert external_id is not None
        assert gross_amount is not None
        assert provider_fee is not None
        assert authorized_net_amount is not None
        assert currency is not None
        existing_payment = db.scalar(
            select(Payment)
            .where(Payment.provider_id == provider.id)
            .where(Payment.external_id == external_id)
            .where(Payment.is_active.is_(True))
        )
        existing_event = db.scalar(
            select(PaymentProviderEvent)
            .where(PaymentProviderEvent.provider_id == provider.id)
            .where(PaymentProviderEvent.external_id == external_id)
        )
        if existing_payment is not None and not _payment_matches_verified_evidence(
            existing_payment,
            intent=intent,
            provider_id=provider.id,
            external_id=external_id,
            gross_amount=gross_amount,
            provider_fee=provider_fee,
            authorized_net_amount=authorized_net_amount,
            currency=currency,
        ):
            disposition = PaystackOutsideWindowRecoveryDisposition.conflict
            evidence_error = "existing_payment_conflict"
        if existing_event is not None:
            event_matches = all(
                (
                    existing_event.source
                    in {
                        PaymentProviderEventSource.verified_webhook,
                        PaymentProviderEventSource.gateway_reconciliation,
                    },
                    existing_event.observation_digest is not None,
                    len(existing_event.observation_digest or "") == 64,
                    existing_event.provider_reference == intent.reference,
                    existing_event.observed_payment_status is PaymentStatus.succeeded,
                    existing_event.financial_effect
                    is PaymentProviderEventFinancialEffect.none,
                    existing_event.status is PaymentProviderEventStatus.processed,
                    existing_event.processed_at is not None,
                    existing_event.error_code is None,
                    existing_event.error is None,
                    existing_event.amount is not None
                    and round_money(existing_event.amount) == gross_amount,
                    round_money(existing_event.provider_fee) == provider_fee,
                    existing_event.net_amount is not None
                    and round_money(existing_event.net_amount) == authorized_net_amount,
                    str(existing_event.currency or "").strip().upper() == currency,
                    existing_event.payment_id is not None,
                    existing_payment is None
                    or existing_event.payment_id == existing_payment.id,
                )
            )
            if not event_matches:
                disposition = PaystackOutsideWindowRecoveryDisposition.conflict
                evidence_error = "existing_provider_event_conflict"

    provider_status = (
        observation.provider_status.value
        if observation.provider_status is not None
        else None
    )
    reason_code = evidence_error or observation.reason_code.value
    material: dict[str, object] = {
        "schema_version": 1,
        "intent": {
            "id": str(intent.id),
            "reference": intent.reference,
            "provider_type": intent.provider_type,
            "provider_id": str(provider.id),
            "checkout_binding_id": (
                str(intent.capability_binding_id)
                if intent.capability_binding_id is not None
                else None
            ),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "billing_account_id": (
                str(intent.billing_account_id) if intent.billing_account_id else None
            ),
            "purpose": intent.purpose,
            "invoice_id": str(intent.invoice_id) if intent.invoice_id else None,
            "status": status.value,
            "created_at": _as_utc(intent.created_at).isoformat(),
            "completed_payment_id": (
                str(intent.completed_payment_id)
                if intent.completed_payment_id is not None
                else None
            ),
            "requested_amount": f"{round_money(intent.requested_amount):.2f}",
            "currency": intent.currency.strip().upper(),
        },
        "policy": {"maximum_age_days": max_age_days},
        "provider_observation": {
            "outcome": observation.outcome.value,
            "provider_status": provider_status,
            "reason_code": observation.reason_code.value,
            "evidence_error": evidence_error,
            "external_id": external_id,
            "gross_amount": (
                f"{gross_amount:.2f}" if gross_amount is not None else None
            ),
            "provider_fee": (
                f"{provider_fee:.2f}" if provider_fee is not None else None
            ),
            "authorized_net_amount": (
                f"{authorized_net_amount:.2f}"
                if authorized_net_amount is not None
                else None
            ),
            "currency": currency,
        },
        "existing_payment": _existing_payment_material(existing_payment),
        "existing_provider_event": _existing_event_material(existing_event),
        "disposition": disposition.value,
    }
    return PaystackOutsideWindowRecoveryPreview(
        intent_id=intent.id,
        reference=intent.reference,
        disposition=disposition,
        actionable=disposition is PaystackOutsideWindowRecoveryDisposition.recoverable,
        fingerprint=_recovery_fingerprint(material),
        intent_status=status,
        intent_created_at=_as_utc(intent.created_at),
        provider_id=provider.id,
        checkout_binding_id=intent.capability_binding_id,
        provider_external_id=external_id,
        gross_amount=gross_amount,
        provider_fee=provider_fee,
        authorized_net_amount=authorized_net_amount,
        currency=currency,
        provider_status=provider_status,
        reason_code=reason_code,
        existing_payment_id=(existing_payment.id if existing_payment else None),
    )


def _observe_paystack_outside_window_recovery(
    db: Session,
    query: PreviewPaystackOutsideWindowRecoveryQuery,
) -> tuple[
    PaystackOutsideWindowRecoveryPreview,
    PaymentGatewayVerificationObservation,
]:

    intent, _status, _max_age_days, _provider = _load_outside_window_intent(db, query)
    checkout_binding_id = intent.capability_binding_id
    db_session_adapter.release_read_transaction(db)
    observation = payment_gateway_adapter.observe_verification(
        db,
        provider_type=PaymentProviderType.paystack.value,
        reference=query.reference,
        capability_binding_id=checkout_binding_id,
    )
    db_session_adapter.release_read_transaction(db)
    current_intent, status, max_age_days, provider = _load_outside_window_intent(
        db,
        query,
    )
    preview = _preview_from_observation(
        db,
        query=query,
        intent=current_intent,
        status=status,
        max_age_days=max_age_days,
        provider=provider,
        observation=observation,
    )
    db_session_adapter.release_read_transaction(db)
    return preview, observation


def preview_paystack_outside_window_recovery(
    db: Session,
    query: PreviewPaystackOutsideWindowRecoveryQuery,
) -> PaystackOutsideWindowRecoveryPreview:
    """Freshly verify one exact outside-window Paystack reference without writes."""

    preview, _observation = _observe_paystack_outside_window_recovery(db, query)
    return preview


def _recovery_command_fingerprint(
    command: ConfirmPaystackOutsideWindowRecoveryCommand,
) -> str:
    return _recovery_fingerprint(
        {
            "schema_version": 1,
            "intent_id": str(command.intent_id),
            "reference": command.reference,
            "preview_fingerprint": command.preview_fingerprint,
            "review_reference": command.review_reference.strip(),
            "actor": command.context.actor.strip(),
            "scope": command.context.scope.strip(),
            "reason": command.context.reason.strip(),
            "idempotency_key": str(command.context.idempotency_key or "").strip(),
        }
    )


def _validate_recovery_command(
    command: ConfirmPaystackOutsideWindowRecoveryCommand,
) -> tuple[str, str, str]:
    if command.context.scope != PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE:
        raise _error(
            "recovery_scope_forbidden",
            "Reviewed Paystack recovery requires its exact authorization scope",
        )
    actor = command.context.actor.strip()
    if not actor or len(actor) > 120:
        raise _error(
            "recovery_actor_invalid",
            "A bounded authenticated finance actor is required",
        )
    if not command.confirmed:
        raise _error(
            "recovery_confirmation_required",
            "Explicit confirmation of the reviewed Paystack recovery is required",
        )
    if len(command.preview_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in command.preview_fingerprint
    ):
        raise _error(
            "recovery_fingerprint_invalid",
            "Preview fingerprint must be a lowercase SHA-256 digest",
        )
    review_reference = command.review_reference.strip()
    if not review_reference or len(review_reference) > 160:
        raise _error(
            "recovery_review_reference_invalid",
            "A bounded finance review or change reference is required",
        )
    reason = command.context.reason.strip()
    if len(reason) < 16:
        raise _error(
            "recovery_reason_invalid",
            "Recovery reason must explain the reviewed financial evidence",
        )
    idempotency_key = str(command.context.idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > 120:
        raise _error(
            "recovery_idempotency_key_invalid",
            "A bounded recovery idempotency key is required",
        )
    return review_reference, reason, idempotency_key


def _recovery_result(
    run: PaystackOutsideWindowRecoveryRun,
    *,
    replayed: bool,
) -> PaystackOutsideWindowRecoveryResult:
    return PaystackOutsideWindowRecoveryResult(
        recovery_run_id=run.id,
        intent_id=run.intent_id,
        disposition=TopupReconciliationDisposition(run.disposition),
        payment_id=run.payment_id,
        preview_fingerprint=run.preview_fingerprint,
        replayed=replayed,
    )


def _idempotent_recovery_result(
    db: Session,
    *,
    idempotency_key: str,
    command_fingerprint: str,
) -> PaystackOutsideWindowRecoveryResult | None:
    run = db.scalar(
        select(PaystackOutsideWindowRecoveryRun).where(
            PaystackOutsideWindowRecoveryRun.idempotency_key == idempotency_key
        )
    )
    if run is None:
        return None
    if run.command_fingerprint != command_fingerprint:
        raise _error(
            "recovery_idempotency_conflict",
            "The idempotency key belongs to different reviewed recovery evidence",
        )
    return _recovery_result(run, replayed=True)


def _recovery_audit_actor_type(actor: str) -> AuditActorType:
    namespace = actor.partition(":")[0]
    return {
        "api_key": AuditActorType.api_key,
        "system": AuditActorType.system,
        "user": AuditActorType.user,
    }.get(namespace, AuditActorType.service)


def _stage_paystack_outside_window_recovery(
    db: Session,
    *,
    command: ConfirmPaystackOutsideWindowRecoveryCommand,
    preview: PaystackOutsideWindowRecoveryPreview,
    observation: PaymentGatewayVerificationObservation,
    command_fingerprint: str,
    review_reference: str,
    reason: str,
    idempotency_key: str,
) -> PaystackOutsideWindowRecoveryResult:
    intent = lock_topup_intent_scope(db, command.intent_id)
    replay = _idempotent_recovery_result(
        db,
        idempotency_key=idempotency_key,
        command_fingerprint=command_fingerprint,
    )
    if replay is not None:
        return replay
    query = PreviewPaystackOutsideWindowRecoveryQuery(
        intent_id=command.intent_id,
        reference=command.reference,
        observed_at=datetime.now(UTC),
    )
    status, max_age_days = _validate_outside_window_intent(
        db,
        query=query,
        intent=intent,
    )
    provider = _active_provider_for_intent(db, intent)
    current = _preview_from_observation(
        db,
        query=query,
        intent=intent,
        status=status,
        max_age_days=max_age_days,
        provider=provider,
        observation=observation,
    )
    if current.fingerprint != preview.fingerprint:
        raise _error(
            "recovery_stale_preview",
            "Local or provider evidence changed after finance preview; preview again",
            expected_fingerprint=preview.fingerprint,
            current_fingerprint=current.fingerprint,
        )
    if not current.actionable or observation.transaction is None:
        raise _error(
            "recovery_not_actionable",
            "Only exact, complete Paystack success evidence can be recovered",
            disposition=current.disposition.value,
        )
    settlement = _stage_verified_settlement_for_intent(
        db,
        ReconcileVerifiedTopupCommand(
            candidate=TopupReconciliationCandidate(
                intent_id=intent.id,
                provider_type=PaymentProviderType.paystack,
                reference=intent.reference,
                status=status,
            ),
            transaction=observation.transaction,
            observed_at=query.observed_at,
        ),
        context=command.context,
        intent=intent,
        invoice_id=intent.invoice_id,
    )
    if settlement.payment_id is None or settlement.provider_event_id is None:
        raise _error(
            "recovery_settlement_incomplete",
            "Canonical settlement omitted its payment or provider-event identity",
        )
    assert current.provider_external_id is not None
    assert current.gross_amount is not None
    assert current.provider_fee is not None
    assert current.authorized_net_amount is not None
    assert current.currency is not None
    run = PaystackOutsideWindowRecoveryRun(
        intent_id=intent.id,
        payment_id=settlement.payment_id,
        provider_event_id=settlement.provider_event_id,
        provider_id=current.provider_id,
        checkout_binding_id=current.checkout_binding_id,
        idempotency_key=idempotency_key,
        command_fingerprint=command_fingerprint,
        preview_fingerprint=current.fingerprint,
        review_reference=review_reference,
        provider_type=PaymentProviderType.paystack.value,
        provider_reference=current.reference,
        external_id=current.provider_external_id,
        gross_amount=current.gross_amount,
        provider_fee=current.provider_fee,
        authorized_net_amount=current.authorized_net_amount,
        currency=current.currency,
        disposition=settlement.disposition.value,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        actor=command.context.actor.strip(),
        reason=reason,
    )
    db.add(run)
    db.flush()
    stage_audit_event(
        db,
        action="recover_paystack_outside_window_topup",
        entity_type="topup_intent",
        entity_id=str(intent.id),
        actor=AuditActor(
            actor_type=_recovery_audit_actor_type(command.context.actor),
            actor_id=command.context.actor.strip(),
        ),
        request_id=str(command.context.correlation_id),
        metadata={
            "owner": "financial.payment_reconciliation",
            "recovery_run_id": str(run.id),
            "review_reference": review_reference,
            "idempotency_key": idempotency_key,
            "preview_fingerprint": current.fingerprint,
            "command_fingerprint": command_fingerprint,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "command_scope": command.context.scope,
            "command_reason": reason,
            "provider_type": PaymentProviderType.paystack.value,
            "provider_id": str(current.provider_id),
            "checkout_binding_id": (
                str(current.checkout_binding_id)
                if current.checkout_binding_id is not None
                else None
            ),
            "provider_reference": current.reference,
            "external_id": current.provider_external_id,
            "gross_amount": f"{current.gross_amount:.2f}",
            "provider_fee": f"{current.provider_fee:.2f}",
            "authorized_net_amount": f"{current.authorized_net_amount:.2f}",
            "currency": current.currency,
            "disposition": settlement.disposition.value,
            "payment_id": str(settlement.payment_id),
            "provider_event_id": str(settlement.provider_event_id),
        },
    )
    emit_event(
        db,
        EventType.paystack_outside_window_recovered,
        {
            "schema_version": 1,
            "recovery_run_id": str(run.id),
            "intent_id": str(intent.id),
            "payment_id": str(settlement.payment_id),
            "provider_event_id": str(settlement.provider_event_id),
            "provider_type": PaymentProviderType.paystack.value,
            "provider_id": str(current.provider_id),
            "checkout_binding_id": (
                str(current.checkout_binding_id)
                if current.checkout_binding_id is not None
                else None
            ),
            "provider_reference": current.reference,
            "external_id": current.provider_external_id,
            "gross_amount": f"{current.gross_amount:.2f}",
            "provider_fee": f"{current.provider_fee:.2f}",
            "authorized_net_amount": f"{current.authorized_net_amount:.2f}",
            "currency": current.currency,
            "disposition": settlement.disposition.value,
            "review_reference": review_reference,
            "preview_fingerprint": current.fingerprint,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        },
        actor=command.context.actor,
        subscriber_id=intent.account_id,
        account_id=intent.account_id,
    )
    return _recovery_result(run, replayed=False)


def confirm_paystack_outside_window_recovery(
    db: Session,
    command: ConfirmPaystackOutsideWindowRecoveryCommand,
) -> PaystackOutsideWindowRecoveryResult:
    """Freshly verify and atomically recover one reviewed Paystack reference."""

    review_reference, reason, idempotency_key = _validate_recovery_command(command)
    command_fingerprint = _recovery_command_fingerprint(command)
    replay = _idempotent_recovery_result(
        db,
        idempotency_key=idempotency_key,
        command_fingerprint=command_fingerprint,
    )
    db_session_adapter.release_read_transaction(db)
    if replay is not None:
        return replay
    preview, observation = _observe_paystack_outside_window_recovery(
        db,
        PreviewPaystackOutsideWindowRecoveryQuery(
            intent_id=command.intent_id,
            reference=command.reference,
            observed_at=datetime.now(UTC),
        ),
    )
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "recovery_stale_preview",
            "Provider or local evidence changed after finance preview; preview again",
            expected_fingerprint=command.preview_fingerprint,
            current_fingerprint=preview.fingerprint,
        )
    if not preview.actionable:
        raise _error(
            "recovery_not_actionable",
            "Only exact, complete Paystack success evidence can be recovered",
            disposition=preview.disposition.value,
        )
    try:
        return execute_owner_command(
            db,
            definition=_PAYSTACK_OUTSIDE_WINDOW_RECOVERY_COMMAND,
            context=command.context,
            operation=lambda: _stage_paystack_outside_window_recovery(
                db,
                command=command,
                preview=preview,
                observation=observation,
                command_fingerprint=command_fingerprint,
                review_reference=review_reference,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
        )
    except IntegrityError:
        # A different intent can race on the globally unique operator key because
        # each command correctly locks only its own intent. The losing root is
        # already rolled back; resolve the committed winner through the same typed
        # replay/conflict contract instead of leaking a storage exception.
        replay = _idempotent_recovery_result(
            db,
            idempotency_key=idempotency_key,
            command_fingerprint=command_fingerprint,
        )
        db_session_adapter.release_read_transaction(db)
        if replay is not None:
            return replay
        raise


def _stage_gateway_observation(
    db: Session,
    command: ReconcileGatewayObservationCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    if command.observation.outcome is PaymentGatewayVerificationOutcome.succeeded:
        raise _error(
            "outcome_invalid",
            "Successful gateway evidence must use the settlement command",
            intent_id=str(command.candidate.intent_id),
        )
    intent = lock_topup_intent_scope(db, command.candidate.intent_id)
    _validate_candidate(intent, command.candidate)
    previous_status = intent.status
    observed_at = _as_utc(command.observed_at)
    next_reconcile_at = _next_gateway_reconcile_at(
        db,
        intent=intent,
        observation=command.observation,
        observed_at=observed_at,
    )
    try:
        result = stage_gateway_topup_observation(
            db,
            RecordGatewayTopupObservationCommand(
                intent_id=intent.id,
                observation=command.observation,
                observed_at=observed_at,
                source=command.source,
                next_reconcile_at=next_reconcile_at,
            ),
            context=_participant_context(
                context,
                scope=INTENT_OBSERVATION_SCOPE,
                reason="Project normalized gateway observation",
                idempotency_key=f"topup-observation-{intent.id}",
            ),
        )
    except TopupIntentError as exc:
        raise _error(
            "topup_projection_rejected",
            exc.message,
            intent_id=str(intent.id),
            topup_error_code=exc.code,
        ) from exc
    disposition = TopupReconciliationDisposition.unchanged
    if previous_status == TopupIntentStatus.pending.value:
        disposition = {
            TopupIntentStatus.expired: TopupReconciliationDisposition.expired,
            TopupIntentStatus.failed: TopupReconciliationDisposition.failed,
            TopupIntentStatus.abandoned: TopupReconciliationDisposition.abandoned,
        }.get(result.status, TopupReconciliationDisposition.unchanged)
    return ReconciledTopupResult(
        intent_id=intent.id,
        payment_id=result.payment_id,
        disposition=disposition,
    )


def record_reconciled_gateway_observation(
    db: Session,
    command: ReconcileGatewayObservationCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    """Commit one normalized non-success observation as an independent root."""

    return execute_owner_command(
        db,
        definition=_GATEWAY_OBSERVATION_COMMAND,
        context=context,
        operation=lambda: _stage_gateway_observation(
            db,
            command,
            context=context,
        ),
    )


def _stage_topup_reconciliation_attempt(
    db: Session,
    *,
    candidate: TopupReconciliationCandidate,
    attempted_at: datetime,
    next_reconcile_at: datetime,
) -> TopupIntentReconciliationAttemptResult:
    try:
        return stage_gateway_topup_reconciliation_attempt(
            db,
            RecordGatewayTopupReconciliationAttemptCommand(
                intent_id=candidate.intent_id,
                expected_provider_type=candidate.provider_type.value,
                expected_reference=candidate.reference,
                expected_status=candidate.status,
                attempted_at=attempted_at,
                next_reconcile_at=next_reconcile_at,
            ),
        )
    except TopupIntentError as exc:
        raise _error(
            "attempt_claim_rejected",
            exc.message,
            intent_id=str(candidate.intent_id),
            topup_error_code=exc.code,
        ) from exc


def claim_topup_reconciliation_attempt(
    db: Session,
    *,
    candidate: TopupReconciliationCandidate,
    attempted_at: datetime,
    next_reconcile_at: datetime,
    context: CommandContext,
) -> TopupIntentReconciliationAttemptResult:
    """Commit the retry lease before making an external gateway call."""

    return execute_owner_command(
        db,
        definition=_RECONCILIATION_ATTEMPT_COMMAND,
        context=context,
        operation=lambda: _stage_topup_reconciliation_attempt(
            db,
            candidate=candidate,
            attempted_at=attempted_at,
            next_reconcile_at=next_reconcile_at,
        ),
    )


def _candidate_context(
    context: CommandContext,
    candidate: TopupReconciliationCandidate,
    *,
    scope: str,
    reason: str,
    idempotency_suffix: str = "settlement",
) -> CommandContext:
    return CommandContext.system(
        actor=context.actor,
        scope=scope,
        reason=reason,
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
        idempotency_key=(
            f"topup-reconciliation-{candidate.intent_id}-{idempotency_suffix}"
        ),
    )


def _provider_lane_candidates(
    db: Session,
    *,
    provider_type: PaymentProviderType,
    statuses: tuple[TopupIntentStatus, ...],
    observed_at: datetime,
    oldest: datetime,
    stale_before: datetime | None,
    limit: int,
) -> tuple[TopupReconciliationCandidate, ...]:
    query = (
        select(
            TopupIntent.id,
            TopupIntent.provider_type,
            TopupIntent.reference,
            TopupIntent.status,
        )
        .where(TopupIntent.status.in_(tuple(status.value for status in statuses)))
        .where(TopupIntent.completed_payment_id.is_(None))
        .where(TopupIntent.provider_type == provider_type.value)
        .where(TopupIntent.created_at > oldest)
        .where(_gateway_reconcile_due(observed_at))
    )
    if stale_before is not None:
        query = query.where(TopupIntent.created_at < stale_before)
    rows = db.execute(
        query.order_by(
            TopupIntent.gateway_last_reconcile_attempt_at.is_not(None).asc(),
            TopupIntent.gateway_last_reconcile_attempt_at.asc(),
            TopupIntent.created_at.asc(),
            TopupIntent.id.asc(),
        ).limit(limit)
    ).all()
    return tuple(
        TopupReconciliationCandidate(
            intent_id=row.id,
            provider_type=parse_supported_provider_type(row.provider_type),
            reference=row.reference,
            status=TopupIntentStatus(row.status),
        )
        for row in rows
    )


def _provider_reconciliation_order(
    db: Session,
) -> tuple[PaymentProviderType, ...]:
    """Start with the provider least recently served by a committed claim."""

    rows = db.execute(
        select(
            TopupIntent.provider_type,
            func.max(TopupIntent.gateway_last_reconcile_attempt_at).label(
                "last_attempt_at"
            ),
        )
        .where(
            TopupIntent.provider_type.in_(
                tuple(provider.value for provider in SUPPORTED_PROVIDER_TYPES)
            )
        )
        .where(TopupIntent.gateway_last_reconcile_attempt_at.is_not(None))
        .group_by(TopupIntent.provider_type)
    ).all()
    last_attempt_by_provider = {
        parse_supported_provider_type(row.provider_type): _as_utc(row.last_attempt_at)
        for row in rows
        if row.last_attempt_at is not None
    }
    stable_order = {
        provider_type: position
        for position, provider_type in enumerate(SUPPORTED_PROVIDER_TYPES)
    }
    never_attempted = datetime.min.replace(tzinfo=UTC)
    return tuple(
        sorted(
            SUPPORTED_PROVIDER_TYPES,
            key=lambda provider_type: (
                provider_type in last_attempt_by_provider,
                last_attempt_by_provider.get(provider_type, never_attempted),
                stable_order[provider_type],
            ),
        )
    )


def _interleaved_lane_candidates(
    db: Session,
    *,
    statuses: tuple[TopupIntentStatus, ...],
    observed_at: datetime,
    oldest: datetime,
    stale_before: datetime | None,
    limit: int,
) -> tuple[TopupReconciliationCandidate, ...]:
    """Interleave queues from the least recently served provider first."""

    provider_rows = tuple(
        _provider_lane_candidates(
            db,
            provider_type=provider_type,
            statuses=statuses,
            observed_at=observed_at,
            oldest=oldest,
            stale_before=stale_before,
            limit=limit,
        )
        for provider_type in _provider_reconciliation_order(db)
    )
    interleaved: list[TopupReconciliationCandidate] = []
    for position in range(limit):
        for rows in provider_rows:
            if position < len(rows):
                interleaved.append(rows[position])
                if len(interleaved) == limit:
                    return tuple(interleaved)
    return tuple(interleaved)


def _lane_capacities(batch_size: int) -> tuple[int, int]:
    """Reserve both lanes while leaving most capacity for customer payments."""

    terminal = max(1, batch_size * _TERMINAL_LANE_PERCENT // 100)
    terminal = min(terminal, batch_size - 1)
    return batch_size - terminal, terminal


def _reconciliation_candidates(
    db: Session,
    *,
    observed_at: datetime,
) -> tuple[TopupReconciliationCandidate, ...]:
    stale_minutes = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_stale_minutes",
    )
    max_age_days = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_max_age_days",
    )
    batch_size = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_batch_size",
    )
    stale_before = observed_at - timedelta(minutes=stale_minutes)
    oldest = observed_at - timedelta(days=max_age_days)
    pending_rows = _interleaved_lane_candidates(
        db,
        statuses=(TopupIntentStatus.pending,),
        observed_at=observed_at,
        oldest=oldest,
        stale_before=stale_before,
        limit=batch_size,
    )
    terminal_rows = _interleaved_lane_candidates(
        db,
        statuses=_TERMINAL_RECOVERY_STATUSES,
        observed_at=observed_at,
        oldest=oldest,
        stale_before=None,
        limit=batch_size,
    )

    pending_capacity, terminal_capacity = _lane_capacities(batch_size)
    pending_count = min(len(pending_rows), pending_capacity)
    terminal_count = min(len(terminal_rows), terminal_capacity)
    remaining = batch_size - pending_count - terminal_count
    pending_extra = min(len(pending_rows) - pending_count, remaining)
    pending_count += pending_extra
    remaining -= pending_extra
    terminal_count += min(len(terminal_rows) - terminal_count, remaining)
    return pending_rows[:pending_count] + terminal_rows[:terminal_count]


def _count_reconcilable(
    db: Session,
    *,
    filters: tuple[ColumnElement[bool], ...],
) -> tuple[int, datetime | None]:
    count, oldest_at = db.execute(
        select(func.count(TopupIntent.id), func.min(TopupIntent.created_at)).where(
            *filters
        )
    ).one()
    return int(count or 0), oldest_at


def topup_reconciliation_backlog(
    db: Session,
    *,
    observed_at: datetime,
    provider_types: tuple[PaymentProviderType, ...] | None = None,
) -> TopupReconciliationBacklog:
    """Project gateway reconciliation work without deciding money consequences."""

    observed_at = _as_utc(observed_at)
    stale_minutes = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_stale_minutes",
    )
    max_age_days = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_max_age_days",
    )
    stale_before = observed_at - timedelta(minutes=stale_minutes)
    oldest_eligible_at = observed_at - timedelta(days=max_age_days)
    selected_providers = (
        SUPPORTED_PROVIDER_TYPES if provider_types is None else provider_types
    )
    supported_values = tuple(item.value for item in selected_providers)
    common = (
        TopupIntent.completed_payment_id.is_(None),
        TopupIntent.provider_type.in_(supported_values),
    )
    pending_base = (
        TopupIntent.status == TopupIntentStatus.pending.value,
        *common,
    )
    terminal_base = (
        TopupIntent.status.in_(
            tuple(status.value for status in _TERMINAL_RECOVERY_STATUSES)
        ),
        *common,
    )
    pending_total, oldest_pending_at = _count_reconcilable(db, filters=pending_base)
    pending_fresh, _ = _count_reconcilable(
        db,
        filters=(*pending_base, TopupIntent.created_at >= stale_before),
    )
    pending_due, oldest_pending_due_created_at = _count_reconcilable(
        db,
        filters=(
            *pending_base,
            TopupIntent.created_at < stale_before,
            TopupIntent.created_at > oldest_eligible_at,
            _gateway_reconcile_due(observed_at),
        ),
    )
    pending_cooling_down, _ = _count_reconcilable(
        db,
        filters=(
            *pending_base,
            TopupIntent.created_at < stale_before,
            TopupIntent.created_at > oldest_eligible_at,
            TopupIntent.gateway_next_reconcile_at.is_not(None),
            TopupIntent.gateway_next_reconcile_at > observed_at,
        ),
    )
    pending_outside_window, _ = _count_reconcilable(
        db,
        filters=(
            *pending_base,
            TopupIntent.created_at <= oldest_eligible_at,
        ),
    )
    terminal_recovery_total, _ = _count_reconcilable(db, filters=terminal_base)
    terminal_recovery_due, oldest_terminal_due_created_at = _count_reconcilable(
        db,
        filters=(
            *terminal_base,
            TopupIntent.created_at > oldest_eligible_at,
            _gateway_reconcile_due(observed_at),
        ),
    )
    terminal_recovery_outside_window, _ = _count_reconcilable(
        db,
        filters=(
            *terminal_base,
            TopupIntent.created_at <= oldest_eligible_at,
        ),
    )
    terminal_recovery_cooling_down, _ = _count_reconcilable(
        db,
        filters=(
            *terminal_base,
            TopupIntent.created_at > oldest_eligible_at,
            TopupIntent.gateway_next_reconcile_at.is_not(None),
            TopupIntent.gateway_next_reconcile_at > observed_at,
        ),
    )
    return TopupReconciliationBacklog(
        pending_total=pending_total,
        pending_fresh=pending_fresh,
        pending_due=pending_due,
        pending_cooling_down=pending_cooling_down,
        pending_outside_window=pending_outside_window,
        terminal_recovery_total=terminal_recovery_total,
        terminal_recovery_due=terminal_recovery_due,
        terminal_recovery_outside_window=terminal_recovery_outside_window,
        terminal_recovery_cooling_down=terminal_recovery_cooling_down,
        oldest_pending_at=oldest_pending_at,
        oldest_pending_due_created_at=oldest_pending_due_created_at,
        oldest_terminal_due_created_at=oldest_terminal_due_created_at,
        stale_before=stale_before,
        oldest_eligible_at=oldest_eligible_at,
    )


def reconcile_pending_topups(
    db: Session,
    command: RunTopupReconciliationCommand,
    *,
    context: CommandContext,
) -> TopupReconciliationSummary:
    """Observe and reconcile one policy-bounded batch of pending intents."""

    observed_at = _as_utc(command.observed_at)
    candidates = _reconciliation_candidates(db, observed_at=observed_at)
    db_session_adapter.release_read_transaction(db)

    checked = checked_pending = checked_terminal = 0
    recovered = linked = expired = failed = abandoned = unchanged = errors = 0
    previous_attempted_at: datetime | None = None
    for candidate in candidates:
        try:
            attempted_at = datetime.now(UTC)
            if (
                previous_attempted_at is not None
                and attempted_at <= previous_attempted_at
            ):
                attempted_at = previous_attempted_at + timedelta(microseconds=1)
            previous_attempted_at = attempted_at
            next_reconcile_at = _attempt_retry_at(
                db,
                candidate=candidate,
                attempted_at=attempted_at,
            )
            db_session_adapter.release_read_transaction(db)
            claim = claim_topup_reconciliation_attempt(
                db,
                candidate=candidate,
                attempted_at=attempted_at,
                next_reconcile_at=next_reconcile_at,
                context=_candidate_context(
                    context,
                    candidate,
                    scope=RECONCILIATION_ATTEMPT_SCOPE,
                    reason="Claim one due gateway reconciliation attempt",
                    idempotency_suffix=(
                        f"attempt-{int(attempted_at.timestamp() * 1_000_000)}"
                    ),
                ),
            )
            if not claim.claimed:
                continue
            checked += 1
            if candidate.status is TopupIntentStatus.pending:
                checked_pending += 1
            else:
                checked_terminal += 1
            observation = payment_gateway_adapter.observe_verification(
                db,
                provider_type=candidate.provider_type.value,
                reference=candidate.reference,
            )
            observation_at = max(datetime.now(UTC), attempted_at)
            db_session_adapter.release_read_transaction(db)
            if observation.outcome is PaymentGatewayVerificationOutcome.succeeded:
                if observation.transaction is None:
                    raise _error(
                        "observation_incomplete",
                        "Successful gateway observation omitted transaction evidence",
                        intent_id=str(candidate.intent_id),
                    )
                result = settle_verified_reconciled_topup(
                    db,
                    ReconcileVerifiedTopupCommand(
                        candidate=candidate,
                        transaction=observation.transaction,
                        observed_at=observation_at,
                    ),
                    context=_candidate_context(
                        context,
                        candidate,
                        scope=VERIFIED_SETTLEMENT_SCOPE,
                        reason="Settle verified stranded top-up",
                    ),
                )
            else:
                result = record_reconciled_gateway_observation(
                    db,
                    ReconcileGatewayObservationCommand(
                        candidate=candidate,
                        observation=observation,
                        observed_at=observation_at,
                        source=GatewayTopupObservationSource.gateway_reconciliation,
                    ),
                    context=_candidate_context(
                        context,
                        candidate,
                        scope=GATEWAY_OBSERVATION_SCOPE,
                        reason="Project normalized stranded top-up observation",
                        idempotency_suffix=(
                            f"{observation.outcome.value}-"
                            f"{int(observation_at.timestamp() * 1_000_000)}"
                        ),
                    ),
                )
        except DomainError as exc:
            logger.warning(
                "Top-up reconciliation rejected intent %s (%s)",
                candidate.intent_id,
                exc.code,
            )
            errors += 1
            continue
        except Exception:
            logger.exception(
                "Top-up reconciliation failed for intent %s",
                candidate.intent_id,
            )
            errors += 1
            continue

        if result.disposition is TopupReconciliationDisposition.recovered:
            recovered += 1
        elif result.disposition is TopupReconciliationDisposition.linked:
            linked += 1
        elif result.disposition is TopupReconciliationDisposition.expired:
            expired += 1
        elif result.disposition is TopupReconciliationDisposition.failed:
            failed += 1
        elif result.disposition is TopupReconciliationDisposition.abandoned:
            abandoned += 1
        elif result.disposition is TopupReconciliationDisposition.unchanged:
            unchanged += 1

    backlog = topup_reconciliation_backlog(db, observed_at=observed_at)
    batch_size = _resolve_reconciliation_int_setting(
        db,
        "topup_reconciliation_batch_size",
    )
    db_session_adapter.release_read_transaction(db)
    due_remaining = backlog.pending_due + backlog.terminal_recovery_due
    saturated = len(candidates) >= batch_size and due_remaining > 0
    partial = errors > 0 or due_remaining > 0

    summary = TopupReconciliationSummary(
        selected=len(candidates),
        checked=checked,
        checked_pending=checked_pending,
        checked_terminal=checked_terminal,
        recovered=recovered,
        linked=linked,
        expired=expired,
        failed=failed,
        abandoned=abandoned,
        unchanged=unchanged,
        errors=errors,
        pending_due_remaining=backlog.pending_due,
        terminal_due_remaining=backlog.terminal_recovery_due,
        outside_window=backlog.outside_window,
        saturated=saturated,
        partial=partial,
    )
    logger.info(
        "Top-up reconciliation completed: selected=%d checked=%d checked_pending=%d "
        "checked_terminal=%d recovered=%d linked=%d expired=%d failed=%d "
        "abandoned=%d unchanged=%d errors=%d pending_due_remaining=%d "
        "terminal_due_remaining=%d outside_window=%d saturated=%s partial=%s",
        summary.selected,
        summary.checked,
        summary.checked_pending,
        summary.checked_terminal,
        summary.recovered,
        summary.linked,
        summary.expired,
        summary.failed,
        summary.abandoned,
        summary.unchanged,
        summary.errors,
        summary.pending_due_remaining,
        summary.terminal_due_remaining,
        summary.outside_window,
        summary.saturated,
        summary.partial,
    )
    return summary
