"""Admin projection for reviewed customer-subledger opening corrections.

``financial.customer_subledger_opening_positions`` remains authoritative for
the opening facts, the correction preview, its enforcement consequence, and
the append-only correction command. This module only parses staff input,
binds one exact owner preview to a short-lived actor-bound confirmation, and
projects the resulting action forms for the account billing page.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
    ActionHiddenValue,
    ActionTone,
)
from app.services.billing.subledger_opening import (
    CORRECTION_SCOPE,
    OWNER,
    CorrectCustomerSubledgerOpeningCommand,
    CustomerSubledgerOpeningAccountView,
    CustomerSubledgerOpeningCorrectionImpact,
    CustomerSubledgerOpeningCorrectionResult,
    CustomerSubledgerOpeningsQuery,
    CustomerSubledgerOpeningSummary,
    OpeningCorrectionEnforcementConsequence,
    PreviewCustomerSubledgerOpeningCorrectionQuery,
    correct_customer_subledger_opening_position,
    list_customer_subledger_openings,
    preview_customer_subledger_opening_correction_impact,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.form_contracts import (
    FormConsequence,
    FormContract,
    FormPrerequisite,
)
from app.services.form_contracts import (
    register as register_form_contract,
)
from app.services.owner_commands import CommandContext

ACTION_KEY = "admin.customer_subledger_opening_correction"
PREVIEW_ACTION_KEY = "admin.customer_subledger_opening_correction.preview"
ACTION_PERMISSION = CORRECTION_SCOPE

_TOKEN_TYPE = "customer_subledger_opening_correction_confirmation"
_TOKEN_ISSUER = "dotmac_sub.admin.customer_subledger_opening_correction"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)
_REASON_MAX = 500
_REVIEW_REFERENCE_MAX = 200
_FIELD_KEYS = ("corrected_opening_amount", "reason", "review_reference")

_OWNER_FIELD_ERRORS = {
    "missing_reason": "reason",
    "missing_review_reference": "review_reference",
    "invalid_corrected_amount": "corrected_opening_amount",
    "no_change": "corrected_opening_amount",
}


CUSTOMER_SUBLEDGER_OPENING_CORRECTION_FORM = register_form_contract(
    FormContract(
        key=ACTION_KEY,
        title="Correct captured opening",
        entity="customer_subledger_opening_position",
        command_owner=OWNER,
        consequences=(
            FormConsequence(
                key="append_only_correction",
                label=(
                    "The original opening is never edited; the owner appends one "
                    "immutable correction with the reviewed before, after, delta, "
                    "reason, and review reference"
                ),
            ),
            FormConsequence(
                key="customer_position",
                label=(
                    "A matching customer-subledger posting changes unapplied "
                    "customer credit by exactly the delta in the same transaction; "
                    "no payment is created"
                ),
            ),
            FormConsequence(
                key="enforcement",
                label=(
                    "Prepaid funding is recalculated from the corrected opening; "
                    "suspension or restoration happens only on the next "
                    "enforcement run, not as part of this correction"
                ),
            ),
        ),
    )
)


class OpeningCorrectionAdminError(DomainError):
    """Safe rejection produced by the admin correction adapter."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise OpeningCorrectionAdminError(
        code=f"admin.customer_subledger_opening_correction.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class OpeningCorrectionFormValues:
    """Raw staff input, parsed once at this adapter boundary."""

    currency: str
    corrected_opening_amount: str = ""
    reason: str = ""
    review_reference: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "corrected_opening_amount": self.corrected_opening_amount,
            "reason": self.reason,
            "review_reference": self.review_reference,
        }


@dataclass(frozen=True, slots=True)
class OpeningCorrectionStaff:
    """Authenticated staff principal resolved by the route adapter."""

    actor: str
    system_user_id: UUID
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class OpeningCorrectionPage:
    account_id: UUID
    currency: str
    opening: CustomerSubledgerOpeningSummary
    entry_form: ActionForm
    form_contract_state: dict[str, object]
    impact: CustomerSubledgerOpeningCorrectionImpact | None = None
    confirm_form: ActionForm | None = None
    effective_at: datetime | None = None
    confirmation_expires_at: datetime | None = None


def account_opening_panel(
    db: Session, *, account_id: UUID
) -> CustomerSubledgerOpeningAccountView | None:
    """Return the owner's opening view, or ``None`` when nothing was captured."""

    view = list_customer_subledger_openings(
        db, query=CustomerSubledgerOpeningsQuery(account_id=account_id)
    )
    return view if view.openings else None


def _field_error_for(error: DomainError) -> str | None:
    field = str(error.details.get("field") or "")
    if field in _FIELD_KEYS:
        return field
    suffix = error.code.rsplit(".", 1)[-1]
    if error.code.startswith(f"{OWNER}."):
        return _OWNER_FIELD_ERRORS.get(suffix)
    return None


def _query(
    *, account_id: UUID, values: OpeningCorrectionFormValues
) -> PreviewCustomerSubledgerOpeningCorrectionQuery:
    raw_amount = values.corrected_opening_amount.strip().replace(",", "")
    if not raw_amount:
        _error(
            "amount_required",
            "Enter the corrected opening amount.",
            field="corrected_opening_amount",
        )
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        _error(
            "invalid_amount",
            "The corrected opening amount must be a number.",
            field="corrected_opening_amount",
        )
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        _error(
            "invalid_amount",
            "The corrected opening amount must be a finite amount with at most "
            "two decimal places.",
            field="corrected_opening_amount",
        )
    reason = values.reason.strip()
    if not reason:
        _error("reason_required", "A correction reason is required.", field="reason")
    if len(reason) > _REASON_MAX:
        _error(
            "reason_too_long",
            f"The reason must be {_REASON_MAX} characters or fewer.",
            field="reason",
        )
    review_reference = values.review_reference.strip()
    if not review_reference:
        _error(
            "review_reference_required",
            "A durable finance review reference is required.",
            field="review_reference",
        )
    if len(review_reference) > _REVIEW_REFERENCE_MAX:
        _error(
            "review_reference_too_long",
            f"The review reference must be {_REVIEW_REFERENCE_MAX} characters "
            "or fewer.",
            field="review_reference",
        )
    return PreviewCustomerSubledgerOpeningCorrectionQuery(
        account_id=account_id,
        currency=values.currency.strip().upper(),
        corrected_opening_amount=amount,
        reason=reason,
        review_reference=review_reference,
    )


def _opening_or_error(
    view: CustomerSubledgerOpeningAccountView, currency: str
) -> CustomerSubledgerOpeningSummary:
    opening = view.opening(currency)
    if opening is None:
        _error(
            "opening_not_found",
            "The account has no captured opening in this currency.",
            currency=currency.strip().upper(),
        )
    return opening


def _form_contract_state(
    view: CustomerSubledgerOpeningAccountView,
    impact: CustomerSubledgerOpeningCorrectionImpact | None,
) -> dict[str, object]:
    prerequisites = [
        FormPrerequisite(
            key="captured_opening",
            label="The account has a captured immutable opening position",
            met=bool(view.openings),
            reason=None if view.openings else view.correction_unavailable_reason,
        ),
        FormPrerequisite(
            key="authority_active",
            label="Customer-subledger authority is active",
            met=view.authority_active,
            reason=None
            if view.authority_active
            else view.correction_unavailable_reason,
        ),
    ]
    if impact is not None:
        prerequisites.append(
            FormPrerequisite(
                key="owner_preview",
                label="The opening owner produced one exact, fingerprinted preview",
                met=True,
            )
        )
    return CUSTOMER_SUBLEDGER_OPENING_CORRECTION_FORM.state(prerequisites)


def _entry_form(
    *,
    account_id: UUID,
    view: CustomerSubledgerOpeningAccountView,
    opening: CustomerSubledgerOpeningSummary,
    values: OpeningCorrectionFormValues,
    field_errors: dict[str, str] | None = None,
    general_error: str | None = None,
    revising: bool = False,
) -> ActionForm:
    errors = field_errors or {}
    mapping = values.as_mapping()
    return ActionForm(
        key=PREVIEW_ACTION_KEY,
        title="Revise correction" if revising else "Correct opening",
        description=(
            f"Enter the reviewed {opening.currency} opening amount that should "
            "replace the current value. Nothing changes until you review the "
            "owner preview and confirm it."
        ),
        action_url=(
            f"/admin/billing/accounts/{account_id}/subledger-opening/"
            f"{opening.currency}/correction/preview"
        ),
        submit_label="Preview correction",
        fields=(
            ActionField(
                key="corrected_opening_amount",
                label=f"Corrected opening amount ({opening.currency})",
                kind=ActionFieldKind.decimal,
                value=mapping["corrected_opening_amount"],
                required=True,
                step="0.01",
                placeholder=f"{opening.current_amount:.2f}",
                help_text=(
                    "Signed amount: positive is customer credit, negative is "
                    f"opening debt. Current value {opening.currency} "
                    f"{opening.current_amount:,.2f}."
                ),
                error=errors.get("corrected_opening_amount"),
            ),
            ActionField(
                key="reason",
                label="Reason",
                kind=ActionFieldKind.textarea,
                value=mapping["reason"],
                required=True,
                max_length=_REASON_MAX,
                rows=3,
                placeholder="Explain the confirmed variance and its evidence.",
                help_text="Retained on the correction, audit, and event evidence.",
                error=errors.get("reason"),
            ),
            ActionField(
                key="review_reference",
                label="Finance review reference",
                kind=ActionFieldKind.text,
                value=mapping["review_reference"],
                required=True,
                max_length=_REVIEW_REFERENCE_MAX,
                placeholder="e.g. FIN-REVIEW-2026-10-09/ACC-12345",
                help_text=(
                    "Durable, non-secret reference to the reviewed evidence "
                    "(ticket, signed worksheet, or statement)."
                ),
                error=errors.get("review_reference"),
            ),
        ),
        hidden_values=(),
        tone=ActionTone.neutral,
        allowed=view.correction_available,
        disabled_reason=view.correction_unavailable_reason,
        general_error=general_error,
    )


def _load(
    db: Session, *, account_id: UUID, currency: str
) -> tuple[CustomerSubledgerOpeningAccountView, CustomerSubledgerOpeningSummary]:
    view = list_customer_subledger_openings(
        db, query=CustomerSubledgerOpeningsQuery(account_id=account_id)
    )
    return view, _opening_or_error(view, currency)


def build_entry_page(
    db: Session,
    *,
    account_id: UUID,
    values: OpeningCorrectionFormValues,
    error: DomainError | None = None,
) -> OpeningCorrectionPage:
    """Project the correction entry form, optionally carrying one rejection."""

    view, opening = _load(db, account_id=account_id, currency=values.currency)
    field = _field_error_for(error) if error is not None else None
    return OpeningCorrectionPage(
        account_id=account_id,
        currency=opening.currency,
        opening=opening,
        entry_form=_entry_form(
            account_id=account_id,
            view=view,
            opening=opening,
            values=values,
            field_errors={field: error.message} if error and field else None,
            general_error=error.message if error and not field else None,
        ),
        form_contract_state=_form_contract_state(view, None),
    )


def _consequence_tone(
    impact: CustomerSubledgerOpeningCorrectionImpact,
) -> ActionTone:
    if (
        impact.enforcement_consequence
        is OpeningCorrectionEnforcementConsequence.suspension_eligible
    ):
        return ActionTone.negative
    if (
        impact.enforcement_consequence
        is OpeningCorrectionEnforcementConsequence.restoration_eligible
    ):
        return ActionTone.positive
    return ActionTone.neutral


def _confirmation_claims(
    *,
    actor: str,
    impact: CustomerSubledgerOpeningCorrectionImpact,
    effective_at: datetime,
    expires_at: datetime,
) -> dict[str, object]:
    preview = impact.preview
    return {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid4().hex,
        "actor": actor,
        "account_id": str(preview.account_id),
        "currency": preview.currency,
        "opening_position_id": str(preview.opening_position_id),
        "preview_fingerprint": preview.preview_fingerprint,
        "iat": int(effective_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }


def build_review_page(
    db: Session,
    *,
    account_id: UUID,
    actor: str,
    values: OpeningCorrectionFormValues,
    general_error: str | None = None,
    now: datetime | None = None,
) -> OpeningCorrectionPage:
    """Project one exact owner preview into a signed, actor-bound confirmation.

    Raises the owner's or this adapter's validation error unchanged; the route
    re-renders the entry form with it.
    """

    normalized_actor = actor.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    view, opening = _load(db, account_id=account_id, currency=values.currency)
    if not view.correction_available:
        _error(
            "correction_unavailable",
            view.correction_unavailable_reason or "Correction is unavailable.",
        )
    query = _query(account_id=account_id, values=values)
    impact = preview_customer_subledger_opening_correction_impact(db, query=query)
    preview = impact.preview
    effective_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    expires_at = effective_at + _TOKEN_TTL
    token = context_signing.sign_context_token(
        db,
        _confirmation_claims(
            actor=normalized_actor,
            impact=impact,
            effective_at=effective_at,
            expires_at=expires_at,
        ),
    )
    currency = preview.currency
    confirm_form = ActionForm(
        key=ACTION_KEY,
        title="Confirm opening correction",
        description=(
            "The owner rechecks this exact preview under an account lock and "
            "refuses it if the opening changed since you reviewed it."
        ),
        action_url=(
            f"/admin/billing/accounts/{account_id}/subledger-opening/"
            f"{currency}/correction/confirm"
        ),
        submit_label=(
            f"Correct opening to {currency} {preview.corrected_opening_amount:,.2f}"
        ),
        fields=(),
        hidden_values=(
            ActionHiddenValue(
                key="corrected_opening_amount",
                value=str(query.corrected_opening_amount),
            ),
            ActionHiddenValue(key="reason", value=query.reason),
            ActionHiddenValue(key="review_reference", value=query.review_reference),
            ActionHiddenValue(
                key="preview_fingerprint", value=preview.preview_fingerprint
            ),
            ActionHiddenValue(key="confirmation_token", value=token),
        ),
        tone=_consequence_tone(impact),
        impact=(
            f"Opening {currency} {preview.previous_opening_amount:,.2f} → "
            f"{currency} {preview.corrected_opening_amount:,.2f} "
            f"(delta {currency} {preview.delta:+,.2f}). {impact.explanation}"
        ),
        confirmation=ActionConfirmation(
            title="I confirm this reviewed correction",
            message=(
                "Finance verified the before, after, and delta values and the "
                "review reference. The original opening stays unchanged and one "
                "append-only correction will be recorded."
            ),
        ),
        general_error=general_error,
    )
    return OpeningCorrectionPage(
        account_id=account_id,
        currency=currency,
        opening=opening,
        entry_form=_entry_form(
            account_id=account_id,
            view=view,
            opening=opening,
            values=values,
            revising=True,
        ),
        form_contract_state=_form_contract_state(view, impact),
        impact=impact,
        confirm_form=confirm_form,
        effective_at=effective_at,
        confirmation_expires_at=expires_at,
    )


def _decode_confirmation(db: Session, token: str) -> dict[Any, Any]:
    normalized = token.strip()
    if not normalized or len(normalized) > 131_072:
        _error(
            "invalid_confirmation",
            "The correction confirmation is invalid; preview again.",
        )
    try:
        claims = context_signing.verify_context_token(db, normalized)
    except JWTError as exc:
        raise OpeningCorrectionAdminError(
            code="admin.customer_subledger_opening_correction.expired_confirmation",
            message="The correction confirmation expired or is invalid; preview again.",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
    ):
        _error(
            "invalid_confirmation",
            "The correction confirmation is invalid; preview again.",
        )
    return claims


def confirm_correction(
    db: Session,
    *,
    account_id: UUID,
    staff: OpeningCorrectionStaff,
    values: OpeningCorrectionFormValues,
    preview_fingerprint: str,
    confirmation_token: str,
    confirmed: str | None,
) -> CustomerSubledgerOpeningCorrectionResult:
    """Validate the staff review envelope and invoke the authoritative owner."""

    actor = staff.actor.strip()
    if not actor:
        _error("actor_required", "An authorized staff actor is required.")
    if confirmed != "yes":
        _error(
            "confirmation_required",
            "Confirm the reviewed correction before continuing.",
        )
    query = _query(account_id=account_id, values=values)
    claims = _decode_confirmation(db, confirmation_token)
    if (
        str(claims.get("account_id") or "") != str(account_id)
        or str(claims.get("currency") or "") != query.currency
        or not hmac.compare_digest(str(claims.get("actor") or ""), actor)
        or not hmac.compare_digest(
            str(claims.get("preview_fingerprint") or ""), preview_fingerprint
        )
    ):
        _error(
            "confirmation_context_changed",
            "The reviewed account, actor, or preview changed; preview again.",
        )
    try:
        token_id = UUID(hex=str(claims["jti"])).hex
    except (KeyError, TypeError, ValueError) as exc:
        raise OpeningCorrectionAdminError(
            code="admin.customer_subledger_opening_correction.invalid_confirmation",
            message="The correction confirmation is invalid; preview again.",
        ) from exc

    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    return correct_customer_subledger_opening_position(
        db,
        command=CorrectCustomerSubledgerOpeningCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=actor,
                scope=CORRECTION_SCOPE,
                reason=query.reason,
                idempotency_key=f"subledger-opening-correction-admin:{token_id}",
            ),
            query=query,
            expected_preview_fingerprint=preview_fingerprint,
            permission_granted=staff.permission_granted,
            authorized_system_user_id=staff.system_user_id,
        ),
    )


def rebuild_after_confirm_error(
    db: Session,
    *,
    account_id: UUID,
    actor: str,
    values: OpeningCorrectionFormValues,
    error: DomainError,
) -> OpeningCorrectionPage:
    """Issue a fresh preview after a refused confirmation, keeping safe input.

    When the same input can no longer be previewed (for example the opening
    already equals it), fall back to the entry form carrying the error.
    """

    try:
        return build_review_page(
            db,
            account_id=account_id,
            actor=actor,
            values=values,
            general_error=error.message,
        )
    except DomainError as preview_error:
        page = build_entry_page(
            db, account_id=account_id, values=values, error=preview_error
        )
        general = (
            error.message
            if page.entry_form.general_error is None
            else f"{error.message} {preview_error.message}"
        )
        return replace(page, entry_form=replace(page.entry_form, general_error=general))


def error_status(error: DomainError) -> int:
    """HTTP status an adapter should use for a correction rejection."""

    if error.code.endswith(
        (".opening_not_found", ".opening_position_not_found", ".account_not_found")
    ):
        return 404
    if error.code.endswith(
        (
            ".stale_reviewed_preview",
            ".expired_confirmation",
            ".confirmation_context_changed",
            ".idempotency_conflict",
            ".active_caller_transaction",
        )
    ):
        return 409
    if error.code.endswith(".permission_denied"):
        return 403
    return 400


__all__ = [
    "ACTION_KEY",
    "ACTION_PERMISSION",
    "CUSTOMER_SUBLEDGER_OPENING_CORRECTION_FORM",
    "PREVIEW_ACTION_KEY",
    "OpeningCorrectionAdminError",
    "OpeningCorrectionFormValues",
    "OpeningCorrectionPage",
    "OpeningCorrectionStaff",
    "account_opening_panel",
    "build_entry_page",
    "build_review_page",
    "confirm_correction",
    "error_status",
    "rebuild_after_confirm_error",
]
