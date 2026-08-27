"""Idempotent repair owner for terminal proofs with submitted top-up intents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentProvider, PaymentStatus, TopupIntent
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services import topup_intents
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

RECONCILIATION_SCOPE = "topup-intent-proof:reconcile"
_MAX_SCAN_LIMIT = 10_000
_RECONCILE_COMMAND = OwnerCommandDefinition(
    owner="financial.topup_intent_proof_reconciliation",
    concern="submitted intent terminal-proof reconciliation",
    name="reconcile_submitted_intent_terminal_proof",
)


class TopupIntentProofReconciliationError(DomainError):
    """Stable rejection from the proof-to-intent repair owner."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> TopupIntentProofReconciliationError:
    return TopupIntentProofReconciliationError(
        code=f"financial.topup_intent_proof_reconciliation.{suffix}",
        message=message,
        details=details,
    )


class TopupIntentProofRepairAction(str, Enum):
    """Closed repair/quarantine classification."""

    complete = "complete"
    cancel = "cancel"
    requires_review = "requires_review"


@dataclass(frozen=True, slots=True)
class TopupIntentProofDriftCandidate:
    """Exact terminal-proof drift evidence discovered read-only."""

    intent_id: UUID
    proof_id: UUID
    proof_status: PaymentProofStatus
    payment_id: UUID | None
    action: TopupIntentProofRepairAction
    review_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileTopupIntentProofCommand:
    """Expected immutable evidence for one bounded repair."""

    intent_id: UUID
    proof_id: UUID
    proof_status: PaymentProofStatus
    payment_id: UUID | None


@dataclass(frozen=True, slots=True)
class TopupIntentProofReconciliationResult:
    """One owner-managed reconciliation outcome."""

    intent_id: UUID
    proof_id: UUID
    status: topup_intents.TopupIntentStatus
    payment_id: UUID | None
    changed: bool


def inspect_terminal_proof_drift(
    db: Session,
    *,
    limit: int = 500,
    scan_limit: int = _MAX_SCAN_LIMIT,
) -> tuple[TopupIntentProofDriftCandidate, ...]:
    """Discover exact proof links without mutating or deciding from account-level joins."""

    if limit < 1 or limit > _MAX_SCAN_LIMIT:
        raise _error(
            "limit_invalid",
            f"Limit must be between 1 and {_MAX_SCAN_LIMIT}",
            limit=limit,
        )
    if scan_limit < limit or scan_limit > _MAX_SCAN_LIMIT:
        raise _error(
            "scan_limit_invalid",
            f"Scan limit must be between limit and {_MAX_SCAN_LIMIT}",
            scan_limit=scan_limit,
        )

    rows = db.execute(
        select(TopupIntent, PaymentProof)
        .join(
            PaymentProof,
            (PaymentProof.account_id == TopupIntent.account_id)
            & (PaymentProof.reference == TopupIntent.reference),
        )
        .where(TopupIntent.status == topup_intents.TopupIntentStatus.submitted.value)
        .where(TopupIntent.provider_type == topup_intents.DIRECT_TRANSFER_PROVIDER)
        .where(
            PaymentProof.status.in_(
                [PaymentProofStatus.verified, PaymentProofStatus.rejected]
            )
        )
        .order_by(TopupIntent.created_at.asc(), TopupIntent.id.asc())
        .limit(scan_limit)
    ).all()

    candidates: list[TopupIntentProofDriftCandidate] = []
    for intent, proof in rows:
        candidate = terminal_proof_drift_for_intent(db, intent)
        if candidate is None or candidate.proof_id != proof.id:
            continue
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return tuple(candidates)


def terminal_proof_drift_for_intent(
    db: Session,
    intent: TopupIntent,
) -> TopupIntentProofDriftCandidate | None:
    """Classify exact terminal-proof drift for one submitted transfer intent."""

    if (
        intent.status != topup_intents.TopupIntentStatus.submitted.value
        or intent.provider_type != topup_intents.DIRECT_TRANSFER_PROVIDER
    ):
        return None
    linked_proof_id = str(
        (intent.metadata_ or {}).get("payment_proof_id") or ""
    ).strip()
    if not linked_proof_id:
        return None
    try:
        proof_id = UUID(linked_proof_id)
    except ValueError:
        return None
    proof = db.get(PaymentProof, proof_id)
    if proof is None:
        return None
    if proof.account_id != intent.account_id or proof.reference != intent.reference:
        return None
    if proof.status not in {
        PaymentProofStatus.verified,
        PaymentProofStatus.rejected,
    }:
        return None
    if proof.status is PaymentProofStatus.rejected:
        return TopupIntentProofDriftCandidate(
            intent_id=intent.id,
            proof_id=proof.id,
            proof_status=proof.status,
            payment_id=proof.payment_id,
            action=TopupIntentProofRepairAction.cancel,
        )

    payment = _current_succeeded_payment_for_intent(db, intent, proof.payment_id)
    if payment is not None:
        return TopupIntentProofDriftCandidate(
            intent_id=intent.id,
            proof_id=proof.id,
            proof_status=proof.status,
            payment_id=proof.payment_id,
            action=TopupIntentProofRepairAction.complete,
        )
    return TopupIntentProofDriftCandidate(
        intent_id=intent.id,
        proof_id=proof.id,
        proof_status=proof.status,
        payment_id=proof.payment_id,
        action=TopupIntentProofRepairAction.requires_review,
        review_reason="verified_proof_payment_not_currently_succeeded",
    )


def _current_succeeded_payment_for_intent(
    db: Session,
    intent: TopupIntent,
    payment_id: UUID | None,
) -> Payment | None:
    if payment_id is None:
        return None
    payment = db.get(Payment, payment_id)
    if payment is None or not payment.is_active:
        return None
    if payment.status is not PaymentStatus.succeeded:
        return None
    if intent.account_id is not None and payment.account_id != intent.account_id:
        return None
    if (
        intent.billing_account_id is not None
        and payment.billing_account_id != intent.billing_account_id
    ):
        return None
    if payment.currency.upper() != intent.currency.upper():
        return None
    if intent.provider_id is not None and payment.provider_id != intent.provider_id:
        return None
    if payment.provider_id is not None:
        provider = db.get(PaymentProvider, payment.provider_id)
        if (
            provider is not None
            and provider.provider_type.value != intent.provider_type
        ):
            return None
    if round_money(to_decimal(payment.amount)) <= 0:
        return None
    return payment


def reconcile_terminal_proof(
    db: Session,
    command: ReconcileTopupIntentProofCommand,
    *,
    context: CommandContext,
) -> TopupIntentProofReconciliationResult:
    """Repair one exact candidate through the canonical intent participant."""

    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMAND,
        context=context,
        operation=lambda: _reconcile_terminal_proof(
            db,
            command=command,
            context=context,
        ),
    )


def _reconcile_terminal_proof(
    db: Session,
    *,
    command: ReconcileTopupIntentProofCommand,
    context: CommandContext,
) -> TopupIntentProofReconciliationResult:
    proof = lock_for_update(db, PaymentProof, command.proof_id)
    if proof is None:
        raise _error(
            "proof_not_found",
            "Payment proof was not found",
            proof_id=str(command.proof_id),
        )
    if proof.status is not command.proof_status:
        raise _error(
            "proof_status_changed",
            "Payment proof status changed after reconciliation preview",
            proof_id=str(proof.id),
            expected_status=command.proof_status.value,
            actual_status=proof.status.value,
        )
    if proof.status not in {
        PaymentProofStatus.verified,
        PaymentProofStatus.rejected,
    }:
        raise _error(
            "proof_not_terminal",
            "Payment proof is not terminal",
            proof_id=str(proof.id),
            status=proof.status.value,
        )
    if proof.payment_id != command.payment_id:
        raise _error(
            "proof_payment_changed",
            "Payment proof link changed after reconciliation preview",
            proof_id=str(proof.id),
            expected_payment_id=(
                str(command.payment_id) if command.payment_id else None
            ),
            actual_payment_id=str(proof.payment_id) if proof.payment_id else None,
        )

    intent = db.get(TopupIntent, command.intent_id)
    if intent is None:
        raise _error(
            "intent_not_found",
            "Top-up intent was not found",
            intent_id=str(command.intent_id),
        )
    if proof.account_id != intent.account_id or proof.reference != intent.reference:
        raise _error(
            "proof_intent_scope_mismatch",
            "Payment proof does not match the top-up intent scope",
            intent_id=str(intent.id),
            proof_id=str(proof.id),
        )

    outcome = (
        topup_intents.DirectTransferProofResolutionOutcome.verified
        if proof.status is PaymentProofStatus.verified
        else topup_intents.DirectTransferProofResolutionOutcome.rejected
    )
    projection = topup_intents.stage_direct_transfer_proof_resolution(
        db,
        topup_intents.ResolveDirectTransferProofCommand(
            intent_id=intent.id,
            proof_id=proof.id,
            outcome=outcome,
            payment_id=proof.payment_id,
            source=(
                topup_intents.DirectTransferProofResolutionSource.payment_proof_reconciliation
            ),
        ),
        context=context,
    )
    return TopupIntentProofReconciliationResult(
        intent_id=projection.intent_id,
        proof_id=projection.proof_id,
        status=projection.status,
        payment_id=projection.payment_id,
        changed=projection.changed,
    )
