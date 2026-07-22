"""Administrative API adapter for subscription billing treatments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import finish_read_response, get_db
from app.schemas.subscription_billing_treatment import (
    BillingTreatmentConfirmRequest,
    BillingTreatmentOutcomeRead,
    BillingTreatmentPreviewRead,
    BillingTreatmentPreviewRequest,
    BillingTreatmentRead,
    BillingTreatmentRevokeRequest,
)
from app.services import subscription_billing_treatments as treatment_service
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

if TYPE_CHECKING:
    from app.models.subscription_billing_treatment import SubscriptionBillingArrangement

router = APIRouter(prefix="/billing-treatments", tags=["billing-treatments"])


def _context(
    principal: Mapping[str, object], *, reason: str, idempotency_key: str
) -> CommandContext:
    principal_id = str(principal.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if principal.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=treatment_service.TREATMENT_WRITE_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _http_error(exc: DomainError) -> HTTPException:
    if exc.code.endswith(("subscription_not_found", "arrangement_not_found")):
        status_code = 404
    elif exc.code.endswith(
        (
            "idempotency_conflict",
            "overlapping_treatment",
            "stale_preview",
            "invalid_transition",
            "active_caller_transaction",
        )
    ):
        status_code = 409
    elif exc.code.endswith(
        (
            "invalid_command",
            "invalid_scope",
            "invalid_treatment",
            "invalid_period",
            "invalid_approval_policy",
            "invalid_currency",
            "retroactive_treatment",
            "finite_period_required",
            "approval_horizon_exceeded",
            "missing_billing_anchor",
            "unaligned_period",
            "unaligned_start",
            "missing_contract_price",
            "missing_sponsor_evidence",
            "subscription_not_collectible",
        )
    ):
        status_code = 422
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _read(arrangement: SubscriptionBillingArrangement) -> BillingTreatmentRead:
    return BillingTreatmentRead(
        arrangement_id=arrangement.id,
        subscription_id=arrangement.subscription_id,
        account_id=arrangement.account_id,
        authorized_offer_id=arrangement.authorized_offer_id,
        treatment=arrangement.treatment,
        reason_code=arrangement.reason_code,
        reason=arrangement.reason,
        starts_at=arrangement.starts_at,
        ends_at=arrangement.ends_at,
        approval_policy_max_days=arrangement.approval_policy_max_days,
        maximum_recurring_amount=arrangement.maximum_recurring_amount,
        billing_cycle=arrangement.billing_cycle,
        currency=arrangement.currency,
        sponsor_reference=arrangement.sponsor_reference,
        cost_center=arrangement.cost_center,
        status=arrangement.status,
        approved_by=arrangement.approved_by,
        approved_at=arrangement.approved_at,
        revoked_by=arrangement.revoked_by,
        revoked_at=arrangement.revoked_at,
        revocation_reason=arrangement.revocation_reason,
    )


@router.get(
    "/subscriptions/{subscription_id}", response_model=list[BillingTreatmentRead]
)
def list_treatments(
    subscription_id: UUID,
    _principal: Mapping[str, object] = Depends(
        require_permission("billing:treatment:read")
    ),
    db: Session = Depends(get_db),
) -> list[BillingTreatmentRead]:
    rows = [
        _read(item)
        for item in treatment_service.list_subscription_billing_arrangements(
            db, subscription_id=subscription_id
        )
    ]
    return finish_read_response(db, rows)


@router.post(
    "/subscriptions/{subscription_id}/preview",
    response_model=BillingTreatmentPreviewRead,
)
def preview_treatment(
    subscription_id: UUID,
    payload: BillingTreatmentPreviewRequest,
    _principal: Mapping[str, object] = Depends(
        require_permission("billing:treatment:write")
    ),
    db: Session = Depends(get_db),
) -> BillingTreatmentPreviewRead:
    try:
        preview = treatment_service.preview_subscription_billing_treatment(
            db,
            subscription_id=subscription_id,
            treatment=payload.treatment,
            reason_code=payload.reason_code,
            reason=payload.reason,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            sponsor_reference=payload.sponsor_reference,
            cost_center=payload.cost_center,
        )
    except DomainError as exc:
        raise _http_error(exc) from exc
    return finish_read_response(db, BillingTreatmentPreviewRead(**asdict(preview)))


@router.post(
    "/subscriptions/{subscription_id}",
    response_model=BillingTreatmentOutcomeRead,
    status_code=status.HTTP_201_CREATED,
)
def create_treatment(
    subscription_id: UUID,
    payload: BillingTreatmentConfirmRequest,
    principal: Mapping[str, object] = Depends(
        require_permission("billing:treatment:write")
    ),
    db: Session = Depends(get_db),
) -> BillingTreatmentOutcomeRead:
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = treatment_service.create_subscription_billing_treatment(
            db,
            treatment_service.CreateBillingTreatmentCommand(
                context=_context(
                    principal,
                    reason="Approved subscription billing treatment",
                    idempotency_key=payload.idempotency_key,
                ),
                subscription_id=subscription_id,
                treatment=payload.treatment,
                reason_code=payload.reason_code,
                reason=payload.reason,
                starts_at=payload.starts_at,
                ends_at=payload.ends_at,
                sponsor_reference=payload.sponsor_reference,
                cost_center=payload.cost_center,
                preview_effective_at=payload.preview_effective_at,
                preview_fingerprint=payload.preview_fingerprint,
            ),
        )
    except DomainError as exc:
        raise _http_error(exc) from exc
    return BillingTreatmentOutcomeRead(**asdict(outcome))


@router.post("/{arrangement_id}/revoke", response_model=BillingTreatmentOutcomeRead)
def revoke_treatment(
    arrangement_id: UUID,
    payload: BillingTreatmentRevokeRequest,
    principal: Mapping[str, object] = Depends(
        require_permission("billing:treatment:write")
    ),
    db: Session = Depends(get_db),
) -> BillingTreatmentOutcomeRead:
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = treatment_service.revoke_subscription_billing_treatment(
            db,
            treatment_service.RevokeBillingTreatmentCommand(
                context=_context(
                    principal,
                    reason="Revoked subscription billing treatment",
                    idempotency_key=payload.idempotency_key,
                ),
                arrangement_id=arrangement_id,
                reason=payload.reason,
            ),
        )
    except DomainError as exc:
        raise _http_error(exc) from exc
    return BillingTreatmentOutcomeRead(**asdict(outcome))
