"""Signed, stale-safe confirmation for staff as-built evidence review.

The vendor operations owner decides eligibility and performs the review
transition. This supporting service owns only proposal integrity and replay;
it deliberately has no HTTP dependency.
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
from app.services import context_signing
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.services.vendor_portal_operations import vendor_portal_operations

_TOKEN_TYPE = "vendor_as_built_review_confirmation"
_TOKEN_ISSUER = "dotmac_sub.vendor_as_built_review_proposals"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)
_SCOPES = {
    "accept": "vendor_as_built_accept",
    "reject": "vendor_as_built_reject",
}


@dataclass(frozen=True)
class VendorAsBuiltReviewProposal:
    as_built_id: str
    project_id: str
    action: str
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    confirmation_label: str
    confirmation_token: str
    expires_at: datetime


@dataclass(frozen=True)
class VendorAsBuiltReviewResult:
    as_built_id: str
    project_id: str
    action: str
    review_event_id: str
    replayed: bool


def _fingerprint(state: dict[str, Any]) -> str:
    encoded = json.dumps(
        state, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def issue_review(
    db: Session,
    *,
    as_built_id: str,
    action: str,
    actor_id: str,
    reason: str | None = None,
) -> VendorAsBuiltReviewProposal:
    normalized_actor = str(actor_id or "").strip()
    if not normalized_actor:
        raise VendorPortalOperationError(
            "actor_required", "Review actor is required", kind="invalid"
        )
    preview = vendor_portal_operations.preview_as_built_review(
        db, as_built_id, action=action, reason=reason
    )
    issued_at = datetime.now(UTC)
    expires_at = issued_at + _TOKEN_TTL
    claims = {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid.uuid4().hex,
        "as_built_id": str(as_built_id),
        "project_id": str(preview["project_id"]),
        "action": action,
        "actor_id": normalized_actor,
        "reason": preview["state"]["reason"],
        "state_fingerprint": _fingerprint(preview["state"]),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return VendorAsBuiltReviewProposal(
        as_built_id=str(as_built_id),
        project_id=str(preview["project_id"]),
        action=action,
        title=str(preview["title"]),
        summary=str(preview["summary"]),
        details=tuple((str(label), str(value)) for label, value in preview["details"]),
        confirmation_label=(
            "Confirm evidence acceptance"
            if action == "accept"
            else "Confirm evidence rejection"
        ),
        confirmation_token=context_signing.sign_context_token(db, claims),
        expires_at=expires_at,
    )


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
        or claims.get("action") not in _SCOPES
    ):
        raise VendorPortalOperationError(
            "invalid_confirmation", "Confirmation proposal is invalid", kind="invalid"
        )
    return claims


def _reserve(db: Session, *, scope: str, key: str) -> IdempotencyKey:
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
            "confirmation_in_progress", "This confirmation is already running"
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
            "confirmation_in_progress", "This confirmation is already running"
        ) from None
    return reservation


def confirm_review(
    db: Session,
    *,
    confirmation_token: str,
    as_built_id: str,
    action: str,
    actor_id: str,
) -> VendorAsBuiltReviewResult:
    claims = _decode(db, confirmation_token)
    if (
        str(claims.get("as_built_id") or "") != str(as_built_id)
        or str(claims.get("action") or "") != str(action)
        or str(claims.get("actor_id") or "") != str(actor_id)
    ):
        raise VendorPortalOperationError(
            "confirmation_context_mismatch",
            "Confirmation proposal belongs to another review context",
            kind="forbidden",
        )
    key = str(claims.get("jti") or "").strip()
    if not key:
        raise VendorPortalOperationError(
            "invalid_confirmation", "Confirmation proposal is invalid", kind="invalid"
        )
    scope = _SCOPES[action]
    reservation = _reserve(db, scope=scope, key=key)
    if reservation.ref_id:
        return VendorAsBuiltReviewResult(
            as_built_id=str(as_built_id),
            project_id=str(claims["project_id"]),
            action=action,
            review_event_id=reservation.ref_id,
            replayed=True,
        )
    try:
        preview = vendor_portal_operations.preview_as_built_review(
            db,
            as_built_id,
            action=action,
            reason=claims.get("reason"),
            for_update=True,
        )
        if not hmac.compare_digest(
            str(claims.get("state_fingerprint") or ""),
            _fingerprint(preview["state"]),
        ):
            raise VendorPortalOperationError(
                "stale_confirmation",
                "As-built evidence changed after preview; review it again",
            )
        result = vendor_portal_operations.transition_as_built_review(
            db,
            as_built_id,
            action=action,
            actor_id=actor_id,
            reason=claims.get("reason"),
            commit=False,
        )
        reservation.ref_id = str(result["review_event_id"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return VendorAsBuiltReviewResult(
        as_built_id=str(as_built_id),
        project_id=str(claims["project_id"]),
        action=action,
        review_event_id=reservation.ref_id,
        replayed=False,
    )
