"""Signed, stale-safe and idempotent vendor submission confirmations.

Domain owners supply the read-only impact snapshot and perform the mutation.
This service owns only the short-lived confirmation proposal and replay guard;
browser routes remain thin adapters.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError
from sqlalchemy.orm import Session

from app.models.idempotency import IdempotencyKey
from app.schemas.vendor_portal import VendorAsBuiltCreate
from app.services import context_signing
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.vendor_portal_operations import (
    StageVendorAsBuiltSubmission,
    StageVendorQuoteSubmission,
    vendor_portal_operations,
)
from app.services.vendor_project_lifecycle import (
    PreviewVendorProjectLifecycle,
    StageVendorProjectTransition,
    VendorProjectLifecycleError,
    preview_project_lifecycle,
    stage_project_transition,
)
from app.services.vendor_purchase_invoices import (
    StageVendorPurchaseInvoiceSubmission,
    vendor_purchase_invoices,
)

_TOKEN_TYPE = "vendor_submission_confirmation"
_TOKEN_ISSUER = "dotmac_sub.vendor_submission_proposals"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)
_SCOPES = {
    "quote": "vendor_quote_submit",
    "as_built": "vendor_as_built_submit",
    "purchase_invoice": "vendor_purchase_invoice_submit",
    "project_start": "vendor_project_start",
    "project_complete": "vendor_project_complete",
}

_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner="operations.vendor_submission_confirmation",
    concern="vendor submission idempotency and replay result",
    name="confirm_vendor_submission",
)


class VendorSubmissionError(DomainError):
    """Stable, transport-neutral vendor submission rejection."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> VendorSubmissionError:
    return VendorSubmissionError(
        code=f"operations.vendor_submission_confirmation.{suffix}",
        message=message,
        details=details,
    )


def _lifecycle_error(exc: VendorProjectLifecycleError) -> VendorSubmissionError:
    suffix = exc.code.rsplit(".", 1)[-1]
    return _error(
        f"lifecycle_{suffix}",
        exc.message,
        lifecycle_code=exc.code,
    )


@dataclass(frozen=True)
class VendorSubmissionProposal:
    submission_type: str
    project_id: str
    target_id: str | None
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    confirmation_label: str
    confirmation_token: str
    expires_at: datetime


@dataclass(frozen=True)
class VendorSubmissionResult:
    submission_type: str
    project_id: str
    result_id: str
    replayed: bool


@dataclass(frozen=True)
class ConfirmVendorSubmissionCommand:
    context: CommandContext
    confirmation_token: str
    vendor_id: str
    user_id: str
    project_id: str


def _fingerprint(state: dict[str, Any]) -> str:
    encoded = json.dumps(
        state,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _issue(
    db: Session,
    preview: dict,
    *,
    vendor_id: str,
    user_id: str,
) -> VendorSubmissionProposal:
    submission_type = str(preview["submission_type"])
    if submission_type not in _SCOPES:
        raise _error(
            "unsupported_submission_type",
            "Vendor submission type is unsupported.",
        )
    issued_at = datetime.now(UTC)
    expires_at = issued_at + _TOKEN_TTL
    claims: dict[str, Any] = {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid.uuid4().hex,
        "submission_type": submission_type,
        "vendor_id": str(vendor_id),
        "user_id": str(user_id),
        "project_id": str(preview["project_id"]),
        "target_id": (str(preview["target_id"]) if preview.get("target_id") else None),
        "state_fingerprint": _fingerprint(preview["state"]),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if submission_type == "as_built":
        claims["payload"] = preview["payload"]
    return VendorSubmissionProposal(
        submission_type=submission_type,
        project_id=str(preview["project_id"]),
        target_id=(str(preview["target_id"]) if preview.get("target_id") else None),
        title=str(preview["title"]),
        summary=str(preview["summary"]),
        details=tuple((str(label), str(value)) for label, value in preview["details"]),
        confirmation_label={
            "quote": "Confirm quote submission",
            "as_built": "Confirm as-built submission",
            "purchase_invoice": "Confirm invoice submission",
            "project_start": "Confirm start",
            "project_complete": "Confirm completion",
        }[submission_type],
        confirmation_token=context_signing.sign_context_token(db, claims),
        expires_at=expires_at,
    )


def issue_quote_submission(
    db: Session,
    *,
    quote_id: str,
    vendor_id: str,
    user_id: str,
) -> VendorSubmissionProposal:
    preview = vendor_portal_operations.preview_quote_submission(db, quote_id, vendor_id)
    return _issue(db, preview, vendor_id=vendor_id, user_id=user_id)


def issue_as_built_submission(
    db: Session,
    *,
    payload: VendorAsBuiltCreate,
    vendor_id: str,
    user_id: str,
) -> VendorSubmissionProposal:
    preview = vendor_portal_operations.preview_as_built_submission(
        db, payload, vendor_id
    )
    return _issue(db, preview, vendor_id=vendor_id, user_id=user_id)


def issue_purchase_invoice_submission(
    db: Session,
    *,
    invoice_id: str,
    vendor_id: str,
    user_id: str,
) -> VendorSubmissionProposal:
    preview = vendor_purchase_invoices.preview_submission(
        db, invoice_id, vendor_id=vendor_id
    )
    return _issue(db, preview, vendor_id=vendor_id, user_id=user_id)


def issue_project_lifecycle(
    db: Session,
    *,
    project_id: str,
    action: str,
    vendor_id: str,
    user_id: str,
) -> VendorSubmissionProposal:
    try:
        preview = preview_project_lifecycle(
            db,
            PreviewVendorProjectLifecycle(
                project_id=project_id,
                vendor_id=vendor_id,
                action=action,
            ),
        )
    except VendorProjectLifecycleError as exc:
        raise _lifecycle_error(exc) from exc
    return _issue(db, preview, vendor_id=vendor_id, user_id=user_id)


def _decode(db: Session, token: str) -> dict[str, Any]:
    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 131_072:
        raise _error("invalid_proposal", "Confirmation proposal is invalid.")
    try:
        claims = context_signing.verify_context_token(db, normalized)
    except JWTError as exc:
        raise _error(
            "expired_proposal",
            "Confirmation proposal is invalid or expired; preview again.",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
        or claims.get("submission_type") not in _SCOPES
    ):
        raise _error("invalid_proposal", "Confirmation proposal is invalid.")
    return claims


def _locked_replay(
    db: Session,
    *,
    scope: str,
    key: str,
) -> IdempotencyKey | None:
    existing = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        if existing.ref_id:
            return existing
        raise _error(
            "confirmation_in_progress",
            "This submission confirmation is already running.",
        )
    return None


def confirm_submission(
    db: Session,
    command: ConfirmVendorSubmissionCommand,
) -> VendorSubmissionResult:
    """Verify, stale-check, reserve, and execute one vendor submission once."""

    def operation() -> VendorSubmissionResult:
        session = db
        claims = _decode(session, command.confirmation_token)
        if (
            str(claims.get("vendor_id") or "") != str(command.vendor_id)
            or str(claims.get("user_id") or "") != str(command.user_id)
            or str(claims.get("project_id") or "") != str(command.project_id)
        ):
            raise _error(
                "proposal_context_mismatch",
                "Confirmation proposal belongs to another context.",
            )
        submission_type = str(claims["submission_type"])
        scope = _SCOPES[submission_type]
        key = str(claims.get("jti") or "").strip()
        if not key:
            raise _error("invalid_proposal", "Confirmation proposal is invalid.")

        prior = (
            session.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
            .one_or_none()
        )
        if prior is not None and prior.ref_id:
            return VendorSubmissionResult(
                submission_type=submission_type,
                project_id=str(command.project_id),
                result_id=prior.ref_id,
                replayed=True,
            )

        target_id = str(claims.get("target_id") or "")
        as_built_payload: VendorAsBuiltCreate | None = None
        if submission_type == "quote":
            preview = vendor_portal_operations.preview_quote_submission(
                session, target_id, command.vendor_id, for_update=True
            )
        elif submission_type == "purchase_invoice":
            preview = vendor_purchase_invoices.preview_submission(
                session,
                target_id,
                vendor_id=command.vendor_id,
                for_update=True,
            )
        elif submission_type == "as_built":
            try:
                as_built_payload = VendorAsBuiltCreate.model_validate(
                    claims.get("payload")
                )
            except (TypeError, ValueError) as exc:
                raise _error(
                    "invalid_payload",
                    "Confirmation proposal payload is invalid.",
                ) from exc
            preview = vendor_portal_operations.preview_as_built_submission(
                session, as_built_payload, command.vendor_id, for_update=True
            )
        else:
            action = "start" if submission_type == "project_start" else "complete"
            try:
                preview = preview_project_lifecycle(
                    session,
                    PreviewVendorProjectLifecycle(
                        project_id=target_id,
                        vendor_id=command.vendor_id,
                        action=action,
                    ),
                    for_update=True,
                )
            except VendorProjectLifecycleError as exc:
                raise _lifecycle_error(exc) from exc

        # The aggregate lock serializes confirmations for this target. Recheck
        # replay evidence after acquiring it so a concurrent exact retry that
        # waited for the first commit returns the original result instead of
        # comparing the now-mutated aggregate with the earlier fingerprint.
        replay = _locked_replay(session, scope=scope, key=key)
        if replay is not None:
            return VendorSubmissionResult(
                submission_type=submission_type,
                project_id=str(command.project_id),
                result_id=str(replay.ref_id),
                replayed=True,
            )
        if not hmac.compare_digest(
            str(claims.get("state_fingerprint") or ""),
            _fingerprint(preview["state"]),
        ):
            raise _error(
                "stale_proposal",
                "Submission data changed after preview; review it again.",
            )

        # Reserve only after stale verification so rejected proposals leave no
        # idempotency row and never require a helper rollback.
        reservation = IdempotencyKey(scope=scope, key=key)
        session.add(reservation)
        session.flush()

        if submission_type == "quote":
            result = vendor_portal_operations.stage_quote_submission(
                session,
                StageVendorQuoteSubmission(
                    context=command.context,
                    quote_id=target_id,
                    vendor_id=command.vendor_id,
                ),
            )
        elif submission_type == "purchase_invoice":
            result = vendor_purchase_invoices.stage_submission(
                session,
                StageVendorPurchaseInvoiceSubmission(
                    context=command.context,
                    invoice_id=target_id,
                    vendor_id=command.vendor_id,
                ),
            )
        elif submission_type == "as_built":
            if as_built_payload is None:
                raise _error(
                    "invalid_payload",
                    "Confirmation proposal payload is invalid.",
                )
            result = vendor_portal_operations.stage_as_built_submission(
                session,
                StageVendorAsBuiltSubmission(
                    context=command.context,
                    payload=as_built_payload,
                    vendor_id=command.vendor_id,
                    user_id=command.user_id,
                ),
            )
        else:
            action = "start" if submission_type == "project_start" else "complete"
            try:
                result = stage_project_transition(
                    session,
                    StageVendorProjectTransition(
                        project_id=target_id,
                        vendor_id=command.vendor_id,
                        action=action,
                        actor_id=command.user_id,
                        actor_type="vendor_user",
                    ),
                )
            except VendorProjectLifecycleError as exc:
                raise _lifecycle_error(exc) from exc
        result_id = str(result.get("lifecycle_event_id") or result.get("id") or "")
        if not result_id:
            raise _error(
                "missing_result_evidence",
                "Vendor submission completed without stable result evidence.",
            )
        reservation.ref_id = result_id
        emit_event(
            session,
            EventType.vendor_submission_confirmed,
            {
                "schema_version": 1,
                "submission_type": submission_type,
                "project_id": str(command.project_id),
                "result_id": result_id,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
            actor=command.context.actor,
        )
        return VendorSubmissionResult(
            submission_type=submission_type,
            project_id=str(command.project_id),
            result_id=result_id,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )
