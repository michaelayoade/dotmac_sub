"""Admin projection for exact paid-invoice prepaid-coverage repair."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID, uuid4

from jose import JWTError
from sqlalchemy.orm import Session

from app.services import context_signing
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
    ActionHiddenValue,
    ActionTone,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.form_contracts import FormConsequence, FormContract, FormPrerequisite
from app.services.form_contracts import register as register_form_contract
from app.services.owner_commands import CommandContext
from app.services.prepaid_coverage_reconciliation import (
    CoverageReconciliationDecision,
    PrepaidCoverageInvoiceReconciliationPreview,
    PrepaidCoverageReconciliationError,
    PrepaidCoverageReconciliationResult,
    ReconcilePrepaidCoverageCommand,
    preview_prepaid_coverage_reconciliation_for_invoice,
    reconcile_prepaid_service_coverage,
)

ACTION_KEY = "admin.prepaid_coverage_reconciliation"
ACTION_PERMISSION = "billing:invoice:update"
_TOKEN_TYPE = "prepaid_coverage_reconciliation_confirmation"
_TOKEN_ISSUER = "dotmac_sub.admin.prepaid_coverage_reconciliation"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)

PREPAID_COVERAGE_RECONCILIATION_FORM = register_form_contract(
    FormContract(
        key=ACTION_KEY,
        title="Repair prepaid service coverage",
        entity="invoice",
        command_owner="financial.prepaid_service_coverage_reconciliation",
        consequences=(
            FormConsequence(
                key="service_entitlement",
                label="Creates only the missing entitlement linked to the exact paid invoice line",
            ),
            FormConsequence(
                key="audit_evidence",
                label="Records immutable reconciliation and event evidence; it does not change payment amounts",
            ),
        ),
    )
)


class PrepaidCoverageAdminError(DomainError):
    """Safe rejection produced by the staff confirmation adapter."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidCoverageAdminError(
        code=f"admin.prepaid_coverage_reconciliation.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PrepaidCoverageInvoiceDetailState:
    review: PrepaidCoverageInvoiceReconciliationPreview
    actionable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PrepaidCoverageAdminReview:
    state: PrepaidCoverageInvoiceDetailState
    confirmation_expires_at: datetime
    action_form: ActionForm
    form_contract_state: dict[str, object]


def _detail_state(
    db: Session, *, invoice_id: UUID, as_of: datetime | None = None
) -> PrepaidCoverageInvoiceDetailState | None:
    review = preview_prepaid_coverage_reconciliation_for_invoice(
        db, invoice_id=invoice_id, as_of=as_of
    )
    items = review.preview.items
    if not items:
        return None
    exact_repair_items = tuple(
        item
        for item in items
        if item.decision is CoverageReconciliationDecision.entitlement_created
        and item.source_id in review.invoice_line_ids
    )
    if len(exact_repair_items) == len(items):
        return PrepaidCoverageInvoiceDetailState(
            review=review,
            actionable=True,
            reason="A paid invoice line has no corresponding prepaid service entitlement.",
        )
    reasons = ", ".join(sorted({item.reason.value for item in items}))
    return PrepaidCoverageInvoiceDetailState(
        review=review,
        actionable=False,
        reason=(
            "Coverage repair is unavailable because the invoice does not provide "
            f"one exact missing-entitlement repair: {reasons}."
        ),
    )


def preview_for_invoice_detail(
    db: Session, *, invoice_id: UUID
) -> PrepaidCoverageInvoiceDetailState | None:
    """Project owner-derived eligibility for the invoice detail page."""

    try:
        return _detail_state(db, invoice_id=invoice_id)
    except PrepaidCoverageReconciliationError:
        return None


def _claims(
    *, actor: str, state: PrepaidCoverageInvoiceDetailState, expires_at: datetime
) -> dict[str, object]:
    preview = state.review.preview
    return {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid4().hex,
        "actor": actor,
        "invoice_id": str(state.review.invoice_id),
        "preview_fingerprint": preview.fingerprint,
        "as_of": int(preview.as_of.timestamp()),
        "iat": int((expires_at - _TOKEN_TTL).timestamp()),
        "exp": int(expires_at.timestamp()),
    }


def _form_contract_state(state: PrepaidCoverageInvoiceDetailState) -> dict[str, object]:
    return PREPAID_COVERAGE_RECONCILIATION_FORM.state(
        [
            FormPrerequisite(
                key="exact_paid_invoice_evidence",
                label="The owner found exact paid invoice evidence for every selected subscription",
                met=state.actionable,
                reason=None if state.actionable else state.reason,
            ),
        ]
    )


def build_admin_review(
    db: Session, *, invoice_id: UUID, actor: str, now: datetime | None = None
) -> PrepaidCoverageAdminReview:
    normalized_actor = actor.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    issued_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    state = _detail_state(db, invoice_id=invoice_id, as_of=issued_at)
    if state is None or not state.actionable:
        _error(
            "repair_not_available",
            "This invoice does not have an exact repairable coverage mismatch.",
        )
    expires_at = issued_at + _TOKEN_TTL
    token = context_signing.sign_context_token(
        db, _claims(actor=normalized_actor, state=state, expires_at=expires_at)
    )
    action_form = ActionForm(
        key=ACTION_KEY,
        title="Repair missing prepaid service coverage",
        description="Confirm the exact paid-invoice evidence before the owner creates a missing service entitlement.",
        action_url=(
            f"/admin/billing/invoices/{invoice_id}/prepaid-coverage-reconciliation/confirm"
        ),
        submit_label="Confirm coverage repair",
        fields=(
            ActionField(
                key="reason",
                label="Operator reason",
                kind=ActionFieldKind.textarea,
                required=True,
                max_length=500,
                rows=3,
                placeholder="Explain the reviewed paid-invoice coverage mismatch.",
                help_text="Required audit evidence; do not include secrets.",
            ),
        ),
        hidden_values=(
            ActionHiddenValue(
                key="preview_fingerprint", value=state.review.preview.fingerprint
            ),
            ActionHiddenValue(key="confirmation_token", value=token),
        ),
        tone=ActionTone.positive,
        impact=(
            "The owner will create only the reviewed entitlement(s), linked to the paid "
            "invoice line(s). It will not alter payment amounts or manually change access."
        ),
        confirmation=ActionConfirmation(
            title="Confirm this exact coverage repair",
            message="I reviewed the paid invoice evidence and understand that the owner will recheck it under lock.",
        ),
        allowed=True,
        disabled_reason=None,
    )
    return PrepaidCoverageAdminReview(
        state=state,
        confirmation_expires_at=expires_at,
        action_form=action_form,
        form_contract_state=_form_contract_state(state),
    )


def _decode_confirmation(db: Session, token: str) -> dict[Any, Any]:
    try:
        claims = context_signing.verify_context_token(db, token.strip())
    except (JWTError, AttributeError) as exc:
        raise PrepaidCoverageAdminError(
            code="admin.prepaid_coverage_reconciliation.expired_confirmation",
            message="The coverage-repair confirmation expired or is invalid; preview again.",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
    ):
        _error(
            "invalid_confirmation",
            "The coverage-repair confirmation is invalid; preview again.",
        )
    return claims


def _validated_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        _error("reason_required", "An operator reason is required.", field="reason")
    if len(normalized) > 500:
        _error(
            "reason_too_long",
            "The operator reason must be 500 characters or fewer.",
            field="reason",
        )
    return normalized


def confirm_admin_review(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    preview_fingerprint: str,
    confirmation_token: str,
    confirmed: str | None,
    reason: str,
) -> PrepaidCoverageReconciliationResult:
    normalized_actor = actor.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    if confirmed != "yes":
        _error(
            "confirmation_required",
            "Confirm the reviewed coverage repair before continuing.",
        )
    claims = _decode_confirmation(db, confirmation_token)
    if (
        str(claims.get("invoice_id") or "") != str(invoice_id)
        or not hmac.compare_digest(str(claims.get("actor") or ""), normalized_actor)
        or not hmac.compare_digest(
            str(claims.get("preview_fingerprint") or ""), preview_fingerprint
        )
    ):
        _error(
            "confirmation_context_changed",
            "The reviewed invoice, actor, or evidence changed; preview again.",
        )
    try:
        as_of = datetime.fromtimestamp(int(claims["as_of"]), tz=UTC)
        token_id = UUID(hex=str(claims["jti"])).hex
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PrepaidCoverageAdminError(
            code="admin.prepaid_coverage_reconciliation.invalid_confirmation",
            message="The coverage-repair confirmation is invalid; preview again.",
        ) from exc
    state = _detail_state(db, invoice_id=invoice_id, as_of=as_of)
    if state is None or not state.actionable:
        _error(
            "repair_not_available",
            "This invoice no longer has an exact repairable coverage mismatch.",
        )
    if not hmac.compare_digest(state.review.preview.fingerprint, preview_fingerprint):
        _error("stale_preview", "The paid-invoice evidence changed; preview again.")
    db_session_adapter.release_read_transaction(db)
    command_id = uuid4()
    return reconcile_prepaid_service_coverage(
        db,
        ReconcilePrepaidCoverageCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=normalized_actor,
                scope=ACTION_PERMISSION,
                reason=_validated_reason(reason),
                idempotency_key=f"prepaid-coverage-admin:{token_id}",
            ),
            as_of=as_of,
            preview_fingerprint=preview_fingerprint,
            subscription_ids=state.review.preview.subscription_ids,
        ),
    )


def rebuild_review_with_error(
    db: Session, *, invoice_id: UUID, actor: str, reason: str, error: DomainError
) -> PrepaidCoverageAdminReview:
    review = build_admin_review(db, invoice_id=invoice_id, actor=actor)
    field = str(error.details.get("field") or "")
    submission = ActionFormSubmission.from_mapping(
        ACTION_KEY,
        {"reason": reason},
        field_errors={field: error.message} if field == "reason" else None,
        general_error=None if field == "reason" else error.message,
    )
    return replace(review, action_form=review.action_form.bind(submission))
