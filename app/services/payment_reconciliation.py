"""Reconcile stranded gateway top-ups from authoritative observations.

The sweep selects immutable candidates and asks the payment transport for one
fact at a time. Each resulting billing consequence is then committed by one
manifest-verified owner command using the existing deposit, provider-event,
and top-up intent participants. Gateway calls never run inside that business
transaction and every intent remains an independent retry boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentProviderType, PaymentStatus, TopupIntent
from app.models.domain_settings import SettingDomain
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
from app.services.billing.providers import PaymentProviders
from app.services.common import round_money, to_decimal
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.payment_gateway_adapter import (
    PaymentGatewayTransaction,
    PaymentGatewayVerificationOutcome,
    payment_gateway_adapter,
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
    EXPIRY_SCOPE,
    CompleteTopupIntentCommand,
    ExpireTopupIntentCommand,
    GatewayTopupIntentFlow,
    TopupIntentCompletionSource,
    TopupIntentError,
    TopupIntentExpirySource,
    TopupIntentStatus,
    lock_topup_intent_scope,
    stage_topup_intent_completion,
    stage_topup_intent_expiry,
)

logger = logging.getLogger(__name__)

RECONCILIATION_SCOPE = "topup-payment:reconcile"
VERIFIED_SETTLEMENT_SCOPE = "topup-payment:reconcile-verified"
UNSUCCESSFUL_OBSERVATION_SCOPE = "topup-payment:reconcile-unsuccessful"

_VERIFIED_SETTLEMENT_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_reconciliation",
    concern="verified provider settlement then allocation orchestration",
    name="settle_verified_reconciled_topup",
)
_UNSUCCESSFUL_OBSERVATION_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_reconciliation",
    concern="stranded top-up reconciliation",
    name="project_unsuccessful_reconciled_topup",
)


class PaymentReconciliationError(DomainError, ValueError):
    """Stable transport-neutral rejection from payment reconciliation."""


class TopupReconciliationDisposition(str, Enum):
    recovered = "recovered"
    linked = "linked"
    expired = "expired"
    unchanged = "unchanged"


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


@dataclass(frozen=True, slots=True)
class ReconcileVerifiedTopupCommand:
    candidate: TopupReconciliationCandidate
    transaction: PaymentGatewayTransaction
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileUnsuccessfulTopupCommand:
    candidate: TopupReconciliationCandidate
    outcome: PaymentGatewayVerificationOutcome
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciledTopupResult:
    intent_id: UUID
    disposition: TopupReconciliationDisposition
    payment_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TopupReconciliationSummary:
    checked: int = 0
    recovered: int = 0
    linked: int = 0
    expired: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        """Serialize the typed result at the Celery transport boundary."""

        return {
            "checked": self.checked,
            "recovered": self.recovered,
            "linked": self.linked,
            "expired": self.expired,
            "errors": self.errors,
        }


@dataclass(frozen=True, slots=True)
class TopupReconciliationBacklog:
    """Read-only projection of pending intents against reconciliation policy."""

    pending: int
    eligible: int
    outside_window: int
    oldest_pending_at: datetime | None
    stale_before: datetime
    oldest_eligible_at: datetime


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
    return external_id, amount, provider_fee, currency


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


def _stage_verified_settlement(
    db: Session,
    command: ReconcileVerifiedTopupCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    external_id, amount, provider_fee, currency = _normalized_transaction(command)
    intent = lock_topup_intent_scope(db, command.candidate.intent_id)
    _validate_candidate(intent, command.candidate)
    if currency != intent.currency.strip().upper():
        raise _error(
            "currency_mismatch",
            "Gateway observation currency does not match the top-up intent",
            intent_id=str(intent.id),
        )

    if intent.completed_payment_id is not None:
        payment = db.get(Payment, intent.completed_payment_id)
        if payment is None or str(payment.external_id or "").strip() != external_id:
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

    provider = PaymentProviders.get_by_type(db, command.candidate.provider_type)
    if provider is None or not provider.is_active:
        raise _error(
            "provider_not_configured",
            "No active payment provider is configured for reconciliation",
            provider=command.candidate.provider_type.value,
        )
    if intent.provider_id is not None and intent.provider_id != provider.id:
        raise _error(
            "provider_configuration_mismatch",
            "Top-up intent is stamped with a different provider configuration",
            intent_id=str(intent.id),
        )

    invoice_id = _target_invoice_id(intent)
    event_key = f"{command.candidate.provider_type.value}-{intent.reference}"
    existing_payment_id = db.scalar(
        select(Payment.id)
        .where(Payment.external_id == external_id)
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
        net_amount=round_money(intent.requested_amount),
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
        disposition=(
            TopupReconciliationDisposition.linked
            if replayed
            else TopupReconciliationDisposition.recovered
        ),
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


def _stage_unsuccessful_observation(
    db: Session,
    command: ReconcileUnsuccessfulTopupCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    if command.outcome not in {
        PaymentGatewayVerificationOutcome.not_found,
        PaymentGatewayVerificationOutcome.not_successful,
    }:
        raise _error(
            "outcome_invalid",
            "Only a definitive unsuccessful observation can expire an intent",
            intent_id=str(command.candidate.intent_id),
        )
    expiry_grace = timedelta(
        hours=_resolve_reconciliation_int_setting(
            db,
            "topup_reconciliation_expiry_grace_hours",
        )
    )
    intent = lock_topup_intent_scope(db, command.candidate.intent_id)
    _validate_candidate(intent, command.candidate)
    if (
        intent.completed_payment_id is not None
        or intent.status != TopupIntentStatus.pending.value
    ):
        return ReconciledTopupResult(
            intent_id=intent.id,
            payment_id=intent.completed_payment_id,
            disposition=TopupReconciliationDisposition.unchanged,
        )
    try:
        result = stage_topup_intent_expiry(
            db,
            ExpireTopupIntentCommand(
                intent_id=intent.id,
                observed_at=_as_utc(command.observed_at),
                grace=expiry_grace,
                source=TopupIntentExpirySource.gateway_reconciliation,
            ),
            context=_participant_context(
                context,
                scope=EXPIRY_SCOPE,
                reason="Project definitive unsuccessful gateway observation",
                idempotency_key=f"topup-expiry-{intent.id}",
            ),
        )
    except TopupIntentError as exc:
        raise _error(
            "topup_projection_rejected",
            exc.message,
            intent_id=str(intent.id),
            topup_error_code=exc.code,
        ) from exc
    return ReconciledTopupResult(
        intent_id=intent.id,
        payment_id=result.payment_id,
        disposition=(
            TopupReconciliationDisposition.expired
            if result.changed
            else TopupReconciliationDisposition.unchanged
        ),
    )


def project_unsuccessful_reconciled_topup(
    db: Session,
    command: ReconcileUnsuccessfulTopupCommand,
    *,
    context: CommandContext,
) -> ReconciledTopupResult:
    """Commit one definitive unsuccessful gateway consequence."""

    return execute_owner_command(
        db,
        definition=_UNSUCCESSFUL_OBSERVATION_COMMAND,
        context=context,
        operation=lambda: _stage_unsuccessful_observation(
            db,
            command,
            context=context,
        ),
    )


def _candidate_context(
    context: CommandContext,
    candidate: TopupReconciliationCandidate,
    *,
    scope: str,
    reason: str,
) -> CommandContext:
    return CommandContext.system(
        actor=context.actor,
        scope=scope,
        reason=reason,
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
        idempotency_key=f"topup-reconciliation-{candidate.intent_id}",
    )


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
    supported_values = tuple(item.value for item in SUPPORTED_PROVIDER_TYPES)
    rows = db.execute(
        select(TopupIntent.id, TopupIntent.provider_type, TopupIntent.reference)
        .where(TopupIntent.status == TopupIntentStatus.pending.value)
        .where(TopupIntent.completed_payment_id.is_(None))
        .where(TopupIntent.provider_type.in_(supported_values))
        .where(TopupIntent.created_at < stale_before)
        .where(TopupIntent.created_at > oldest)
        .order_by(TopupIntent.created_at.asc(), TopupIntent.id.asc())
        .limit(batch_size)
    ).all()
    return tuple(
        TopupReconciliationCandidate(
            intent_id=row.id,
            provider_type=parse_supported_provider_type(row.provider_type),
            reference=row.reference,
        )
        for row in rows
    )


def topup_reconciliation_backlog(
    db: Session,
    *,
    observed_at: datetime,
) -> TopupReconciliationBacklog:
    """Project pending gateway intents without deciding a money consequence."""

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
    supported_values = tuple(item.value for item in SUPPORTED_PROVIDER_TYPES)
    base = (
        TopupIntent.status == TopupIntentStatus.pending.value,
        TopupIntent.completed_payment_id.is_(None),
        TopupIntent.provider_type.in_(supported_values),
    )
    pending, oldest_pending_at = db.execute(
        select(func.count(TopupIntent.id), func.min(TopupIntent.created_at)).where(
            *base
        )
    ).one()
    eligible = db.scalar(
        select(func.count(TopupIntent.id))
        .where(*base)
        .where(TopupIntent.created_at < stale_before)
        .where(TopupIntent.created_at > oldest_eligible_at)
    )
    outside_window = db.scalar(
        select(func.count(TopupIntent.id))
        .where(*base)
        .where(TopupIntent.created_at <= oldest_eligible_at)
    )
    return TopupReconciliationBacklog(
        pending=int(pending or 0),
        eligible=int(eligible or 0),
        outside_window=int(outside_window or 0),
        oldest_pending_at=oldest_pending_at,
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

    recovered = linked = expired = errors = 0
    for candidate in candidates:
        observation = payment_gateway_adapter.observe_verification(
            db,
            provider_type=candidate.provider_type.value,
            reference=candidate.reference,
        )
        db_session_adapter.release_read_transaction(db)
        try:
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
                        observed_at=observed_at,
                    ),
                    context=_candidate_context(
                        context,
                        candidate,
                        scope=VERIFIED_SETTLEMENT_SCOPE,
                        reason="Settle verified stranded top-up",
                    ),
                )
            elif observation.outcome in {
                PaymentGatewayVerificationOutcome.not_found,
                PaymentGatewayVerificationOutcome.not_successful,
            }:
                result = project_unsuccessful_reconciled_topup(
                    db,
                    ReconcileUnsuccessfulTopupCommand(
                        candidate=candidate,
                        outcome=observation.outcome,
                        observed_at=observed_at,
                    ),
                    context=_candidate_context(
                        context,
                        candidate,
                        scope=UNSUCCESSFUL_OBSERVATION_SCOPE,
                        reason="Project unsuccessful stranded top-up observation",
                    ),
                )
            else:
                logger.warning(
                    "Top-up reconciliation provider unavailable for intent %s (%s)",
                    candidate.intent_id,
                    observation.error_code or "unknown",
                )
                errors += 1
                continue
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

    summary = TopupReconciliationSummary(
        checked=len(candidates),
        recovered=recovered,
        linked=linked,
        expired=expired,
        errors=errors,
    )
    logger.info(
        "Top-up reconciliation completed: checked=%d recovered=%d linked=%d "
        "expired=%d errors=%d",
        summary.checked,
        summary.recovered,
        summary.linked,
        summary.expired,
        summary.errors,
    )
    return summary
