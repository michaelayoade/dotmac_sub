"""Administrative manual-payment duplicate control and coordination.

This owner composes the canonical payment and payment-proof records without
moving either authority. It owns the cross-domain decision that a staff-entered
confirmed payment must carry a unique account-scoped reference and that
same-amount evidence must be reviewed before settlement is recorded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import Payment, PaymentStatus
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    ManualPaymentRecordingConfirm,
    ManualPaymentRecordingPreviewRequest,
    PaymentCreate,
    PaymentCreationConfirm,
)
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.billing.payments import (
    PaymentCreationPreview,
    PaymentCreationResult,
    Payments,
)
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.manual_payment_recording"
MANUAL_PAYMENT_RECORDING_SCOPE = "billing:payment:create"
_RECENT_PAYMENT_HORIZON = timedelta(days=90)
_MAX_RISKS_PER_SOURCE = 5
_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="locked administrative manual-payment confirmation",
    name="confirm_manual_payment_recording",
)


class ManualPaymentDuplicateRiskKind(StrEnum):
    existing_payment_same_amount = "existing_payment_same_amount"
    submitted_proof_same_amount = "submitted_proof_same_amount"


@dataclass(frozen=True, slots=True)
class ManualPaymentDuplicateRisk:
    kind: ManualPaymentDuplicateRiskKind
    evidence_id: UUID
    evidence_status: PaymentStatus | PaymentProofStatus
    amount: Decimal
    currency: str
    reference: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ManualPaymentDuplicateAssessment:
    normalized_reference: str | None
    risks: tuple[ManualPaymentDuplicateRisk, ...]

    @property
    def requires_acknowledgement(self) -> bool:
        return bool(self.risks)


@dataclass(frozen=True, slots=True)
class ManualPaymentRecordingPreview:
    payment_preview: PaymentCreationPreview
    duplicate_risks: tuple[ManualPaymentDuplicateRisk, ...]
    requires_duplicate_acknowledgement: bool
    control_fingerprint: str


@dataclass(frozen=True, slots=True)
class ManualPaymentRecordingResult:
    payment_result: PaymentCreationResult
    preview: ManualPaymentRecordingPreview | None

    @property
    def idempotent_replay(self) -> bool:
        return self.payment_result.idempotent_replay


class ManualPaymentRecordingError(DomainError):
    """Stable administrative payment-control failure."""


def _error(suffix: str, message: str, **details: object) -> ManualPaymentRecordingError:
    return ManualPaymentRecordingError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _normalized_reference(value: str | None) -> str | None:
    normalized = (value or "").strip().casefold()
    return normalized or None


def _assessment(
    db: Session,
    *,
    account_id: UUID,
    amount: Decimal,
    currency: str,
    status: PaymentStatus,
    reference: str | None,
) -> ManualPaymentDuplicateAssessment:
    normalized_reference = _normalized_reference(reference)
    if status is not PaymentStatus.succeeded:
        return ManualPaymentDuplicateAssessment(
            normalized_reference=normalized_reference,
            risks=(),
        )
    if normalized_reference is None:
        raise _error(
            "reference_required",
            "A confirmed manual payment requires a bank transaction or receipt reference.",
            account_id=str(account_id),
        )

    normalized_currency = currency.strip().upper()
    normalized_amount = round_money(to_decimal(amount))
    payment_reference_match = db.scalar(
        select(Payment.id)
        .where(
            Payment.account_id == account_id,
            Payment.currency == normalized_currency,
            Payment.external_id.is_not(None),
            func.lower(func.trim(Payment.external_id)) == normalized_reference,
        )
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    if payment_reference_match is not None:
        raise _error(
            "reference_already_recorded",
            "This payment reference is already recorded for the customer. Review the existing payment instead of creating another one.",
            account_id=str(account_id),
            payment_id=str(payment_reference_match),
        )

    submitted_reference_match = db.scalar(
        select(PaymentProof.id)
        .where(
            PaymentProof.account_id == account_id,
            PaymentProof.currency == normalized_currency,
            PaymentProof.status == PaymentProofStatus.submitted,
            PaymentProof.reference.is_not(None),
            func.lower(func.trim(PaymentProof.reference)) == normalized_reference,
        )
        .order_by(PaymentProof.created_at.asc())
        .limit(1)
    )
    if submitted_reference_match is not None:
        raise _error(
            "reference_has_submitted_proof",
            "A submitted payment proof already carries this reference. Review that proof instead of recording a separate payment.",
            account_id=str(account_id),
            proof_id=str(submitted_reference_match),
        )

    recent_after = datetime.now(UTC) - _RECENT_PAYMENT_HORIZON
    payment_rows = db.scalars(
        select(Payment)
        .where(
            Payment.account_id == account_id,
            Payment.currency == normalized_currency,
            Payment.amount == normalized_amount,
            Payment.is_active.is_(True),
            Payment.created_at >= recent_after,
            Payment.status.notin_((PaymentStatus.failed, PaymentStatus.canceled)),
        )
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(_MAX_RISKS_PER_SOURCE)
    ).all()
    proof_rows = db.scalars(
        select(PaymentProof)
        .where(
            PaymentProof.account_id == account_id,
            PaymentProof.currency == normalized_currency,
            PaymentProof.amount == normalized_amount,
            PaymentProof.status == PaymentProofStatus.submitted,
        )
        .order_by(PaymentProof.created_at.desc(), PaymentProof.id.desc())
        .limit(_MAX_RISKS_PER_SOURCE)
    ).all()
    risks = tuple(
        [
            ManualPaymentDuplicateRisk(
                kind=ManualPaymentDuplicateRiskKind.existing_payment_same_amount,
                evidence_id=row.id,
                evidence_status=row.status,
                amount=round_money(to_decimal(row.amount)),
                currency=row.currency,
                reference=row.external_id,
                observed_at=row.paid_at or row.created_at,
            )
            for row in payment_rows
        ]
        + [
            ManualPaymentDuplicateRisk(
                kind=ManualPaymentDuplicateRiskKind.submitted_proof_same_amount,
                evidence_id=row.id,
                evidence_status=row.status,
                amount=round_money(to_decimal(row.amount)),
                currency=row.currency,
                reference=row.reference,
                observed_at=row.paid_at or row.created_at,
            )
            for row in proof_rows
        ]
    )
    return ManualPaymentDuplicateAssessment(
        normalized_reference=normalized_reference,
        risks=risks,
    )


def _control_fingerprint(
    payment_preview: PaymentCreationPreview,
    assessment: ManualPaymentDuplicateAssessment,
) -> str:
    encoded = json.dumps(
        {
            "kind": "manual_payment_recording",
            "payment_preview_fingerprint": payment_preview.fingerprint,
            "normalized_reference": assessment.normalized_reference,
            "duplicate_risks": [
                {
                    "kind": risk.kind.value,
                    "evidence_id": str(risk.evidence_id),
                    "evidence_status": risk.evidence_status.value,
                    "amount": f"{risk.amount:.2f}",
                    "currency": risk.currency,
                    "reference": _normalized_reference(risk.reference),
                    "observed_at": risk.observed_at.isoformat(),
                }
                for risk in assessment.risks
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preview_manual_payment_recording(
    db: Session,
    request: ManualPaymentRecordingPreviewRequest,
) -> ManualPaymentRecordingPreview:
    payment_preview = Payments.preview_creation(db, request)
    assert request.account_id is not None
    assessment = _assessment(
        db,
        account_id=request.account_id,
        amount=request.amount,
        currency=request.currency,
        status=request.status,
        reference=request.external_id,
    )
    return ManualPaymentRecordingPreview(
        payment_preview=payment_preview,
        duplicate_risks=assessment.risks,
        requires_duplicate_acknowledgement=assessment.requires_acknowledgement,
        control_fingerprint=_control_fingerprint(payment_preview, assessment),
    )


def _request_payload(request: ManualPaymentRecordingConfirm) -> PaymentCreate:
    return PaymentCreate(
        **request.model_dump(
            exclude={
                "auto_allocate",
                "preview_fingerprint",
                "idempotency_key",
                "control_fingerprint",
                "duplicate_risk_acknowledged",
            }
        )
    )


def _preview_request(
    request: ManualPaymentRecordingConfirm,
) -> ManualPaymentRecordingPreviewRequest:
    return ManualPaymentRecordingPreviewRequest(
        **request.model_dump(
            exclude={
                "preview_fingerprint",
                "idempotency_key",
                "control_fingerprint",
                "duplicate_risk_acknowledged",
            }
        )
    )


def _audit_actor(context: CommandContext) -> tuple[AuditActorType, str]:
    actor_type_value, separator, actor_id = context.actor.partition(":")
    if separator:
        try:
            return AuditActorType(actor_type_value), actor_id[:120]
        except ValueError:
            pass
    return AuditActorType.system, context.actor[:120]


def _validate_command(
    context: CommandContext,
    request: ManualPaymentRecordingConfirm,
) -> None:
    if context.scope != MANUAL_PAYMENT_RECORDING_SCOPE:
        raise _error(
            "invalid_scope",
            "Manual payment recording requires billing payment-create authority.",
        )
    if context.idempotency_key != request.idempotency_key:
        raise _error(
            "idempotency_conflict",
            "Command and payment idempotency evidence do not match.",
        )


def confirm_manual_payment_recording(
    db: Session,
    *,
    context: CommandContext,
    request: ManualPaymentRecordingConfirm,
) -> ManualPaymentRecordingResult:
    _validate_command(context, request)

    def operation() -> ManualPaymentRecordingResult:
        payment_payload = _request_payload(request)
        replay = Payments.replay_creation_request(
            db,
            payment_payload,
            auto_allocate=request.auto_allocate,
            idempotency_key=request.idempotency_key,
        )
        if replay is not None:
            return ManualPaymentRecordingResult(
                payment_result=replay,
                preview=None,
            )

        assert request.account_id is not None
        lock_account(db, str(request.account_id))
        preview = preview_manual_payment_recording(db, _preview_request(request))
        if preview.payment_preview.fingerprint != request.preview_fingerprint:
            raise _error(
                "stale_payment_preview",
                "Financial state changed after preview; preview the payment again.",
            )
        if preview.control_fingerprint != request.control_fingerprint:
            raise _error(
                "stale_duplicate_evidence",
                "Matching payment or proof evidence changed after preview; review it again.",
            )
        if (
            preview.requires_duplicate_acknowledgement
            and not request.duplicate_risk_acknowledged
        ):
            raise _error(
                "duplicate_risk_acknowledgement_required",
                "Review and acknowledge the matching payment or proof evidence before recording this payment.",
                risk_count=len(preview.duplicate_risks),
            )

        payment_result = Payments.stage_confirm_creation(
            db,
            PaymentCreationConfirm(
                **payment_payload.model_dump(),
                auto_allocate=request.auto_allocate,
                preview_fingerprint=preview.payment_preview.fingerprint,
                idempotency_key=request.idempotency_key,
            ),
        )
        actor_type, actor_id = _audit_actor(context)
        risk_ids = [str(risk.evidence_id) for risk in preview.duplicate_risks]
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=actor_type,
                actor_id=actor_id,
                action="confirm_manual_payment_recording",
                entity_type="payment",
                entity_id=str(payment_result.payment.id),
                request_id=str(context.correlation_id),
                metadata_={
                    "owner": OWNER,
                    "command_id": str(context.command_id),
                    "control_fingerprint": preview.control_fingerprint,
                    "duplicate_risk_acknowledged": bool(preview.duplicate_risks),
                    "duplicate_risk_ids": risk_ids,
                },
            ),
        )
        emit_event(
            db,
            EventType.manual_payment_recorded,
            {
                "aggregate_type": "payment",
                "aggregate_id": str(payment_result.payment.id),
                "aggregate_version": str(context.command_id),
                "account_id": str(request.account_id),
                "control_fingerprint": preview.control_fingerprint,
                "duplicate_risk_acknowledged": bool(preview.duplicate_risks),
                "duplicate_risk_ids": risk_ids,
            },
            actor=context.actor,
            account_id=request.account_id,
        )
        return ManualPaymentRecordingResult(
            payment_result=payment_result,
            preview=preview,
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=context,
        operation=operation,
    )


__all__ = [
    "MANUAL_PAYMENT_RECORDING_SCOPE",
    "ManualPaymentDuplicateRisk",
    "ManualPaymentDuplicateRiskKind",
    "ManualPaymentRecordingError",
    "ManualPaymentRecordingPreview",
    "ManualPaymentRecordingResult",
    "confirm_manual_payment_recording",
    "preview_manual_payment_recording",
]
