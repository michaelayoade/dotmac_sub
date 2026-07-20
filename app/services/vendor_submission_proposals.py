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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.idempotency import IdempotencyKey
from app.schemas.vendor_portal import VendorAsBuiltCreate
from app.services import context_signing
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.services.vendor_portal_operations import vendor_portal_operations
from app.services.vendor_purchase_invoices import vendor_purchase_invoices

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
        raise ValueError("Unsupported vendor submission type")
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
    preview = vendor_portal_operations.preview_project_lifecycle(
        db, project_id, vendor_id=vendor_id, action=action
    )
    return _issue(db, preview, vendor_id=vendor_id, user_id=user_id)


def _decode(db: Session, token: str) -> dict[str, Any]:
    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 131_072:
        raise VendorPortalOperationError(
            "invalid_confirmation", "Confirmation proposal is invalid", kind="invalid"
        )
    try:
        claims = context_signing.verify_context_token(db, normalized)
    except JWTError as exc:
        raise VendorPortalOperationError(
            "expired_confirmation",
            "Confirmation proposal is invalid or expired; preview again",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
        or claims.get("submission_type") not in _SCOPES
    ):
        raise VendorPortalOperationError(
            "invalid_confirmation", "Confirmation proposal is invalid", kind="invalid"
        )
    return claims


def _replay_or_reserve(
    db: Session,
    *,
    scope: str,
    key: str,
) -> IdempotencyKey:
    existing = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        if existing.ref_id:
            return existing
        raise VendorPortalOperationError(
            "confirmation_in_progress",
            "This submission confirmation is already running",
        )
    reservation = IdempotencyKey(scope=scope, key=key)
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        replay = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
            .one_or_none()
        )
        if replay is not None and replay.ref_id:
            return replay
        raise VendorPortalOperationError(
            "confirmation_in_progress",
            "This submission confirmation is already running",
        ) from None
    return reservation


def confirm_submission(
    db: Session,
    *,
    confirmation_token: str,
    vendor_id: str,
    user_id: str,
    project_id: str,
) -> VendorSubmissionResult:
    """Verify, stale-check, reserve, and execute one vendor submission once."""
    claims = _decode(db, confirmation_token)
    if (
        str(claims.get("vendor_id") or "") != str(vendor_id)
        or str(claims.get("user_id") or "") != str(user_id)
        or str(claims.get("project_id") or "") != str(project_id)
    ):
        raise VendorPortalOperationError(
            "confirmation_context_mismatch",
            "Confirmation proposal belongs to another context",
            kind="forbidden",
        )
    submission_type = str(claims["submission_type"])
    scope = _SCOPES[submission_type]
    key = str(claims.get("jti") or "").strip()
    if not key:
        raise VendorPortalOperationError(
            "invalid_confirmation", "Confirmation proposal is invalid", kind="invalid"
        )

    prior = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .one_or_none()
    )
    if prior is not None and prior.ref_id:
        return VendorSubmissionResult(
            submission_type=submission_type,
            project_id=str(project_id),
            result_id=prior.ref_id,
            replayed=True,
        )

    reservation = _replay_or_reserve(db, scope=scope, key=key)
    if reservation.ref_id:
        return VendorSubmissionResult(
            submission_type=submission_type,
            project_id=str(project_id),
            result_id=reservation.ref_id,
            replayed=True,
        )
    target_id = str(claims.get("target_id") or "")
    try:
        if submission_type == "quote":
            preview = vendor_portal_operations.preview_quote_submission(
                db, target_id, vendor_id, for_update=True
            )
        elif submission_type == "purchase_invoice":
            preview = vendor_purchase_invoices.preview_submission(
                db, target_id, vendor_id=vendor_id, for_update=True
            )
        elif submission_type == "as_built":
            try:
                payload = VendorAsBuiltCreate.model_validate(claims.get("payload"))
            except (TypeError, ValueError) as exc:
                raise VendorPortalOperationError(
                    "invalid_confirmation_payload",
                    "Confirmation proposal payload is invalid",
                    kind="invalid",
                ) from exc
            preview = vendor_portal_operations.preview_as_built_submission(
                db, payload, vendor_id, for_update=True
            )
        else:
            action = "start" if submission_type == "project_start" else "complete"
            preview = vendor_portal_operations.preview_project_lifecycle(
                db,
                target_id,
                vendor_id=vendor_id,
                action=action,
                for_update=True,
            )
        if not hmac.compare_digest(
            str(claims.get("state_fingerprint") or ""),
            _fingerprint(preview["state"]),
        ):
            raise VendorPortalOperationError(
                "stale_confirmation",
                "Submission data changed after preview; review it again",
            )
        if submission_type == "quote":
            result = vendor_portal_operations.submit_quote(
                db, target_id, vendor_id, commit=False
            )
        elif submission_type == "purchase_invoice":
            result = vendor_purchase_invoices.submit(
                db, target_id, vendor_id=vendor_id, commit=False
            )
        elif submission_type == "as_built":
            result = vendor_portal_operations.submit_as_built(
                db, payload, vendor_id, user_id, commit=False
            )
        else:
            action = "start" if submission_type == "project_start" else "complete"
            result = vendor_portal_operations.transition_project(
                db,
                target_id,
                vendor_id=vendor_id,
                action=action,
                actor_id=user_id,
                actor_type="vendor_user",
                commit=False,
            )
        reservation.ref_id = str(result.get("lifecycle_event_id") or result.get("id"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return VendorSubmissionResult(
        submission_type=submission_type,
        project_id=str(project_id),
        result_id=reservation.ref_id,
        replayed=False,
    )
