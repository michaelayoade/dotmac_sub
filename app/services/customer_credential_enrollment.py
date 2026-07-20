"""Passwordless invitation into customer-owned local credential enrollment.

Referral signup creates an account but no placeholder credential. This owner
queues a non-secret action, mints its purpose-bound capability only at delivery,
and creates the local credential only after the recipient supplies a password.
Completing enrollment verifies the account email; it does not activate, merge,
or verify the account's Party identity.
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from jose import JWTError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import AuthProvider, UserCredential
from app.models.notification import Notification, NotificationChannel
from app.models.referral_native import Referral
from app.models.sales import Lead
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import auth_cache, context_signing
from app.services import auth_flow as auth_flow_service
from app.services import email as email_service
from app.services.audit_adapter import record_audit_event, stage_audit_event
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import (
    submit as submit_communication_intent,
)
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    REFERRAL_CREDENTIAL_ENROLLMENT_ACTION,
    EphemeralActionRejected,
    EphemeralEmailContent,
)
from app.services.ephemeral_communication_actions import (
    descriptor as ephemeral_action_descriptor,
)
from app.services.rate_limiter_adapter import allow_operation

EnrollmentDeliveryStatus = Literal[
    "queued",
    "rate_limited",
    "suppressed",
    "already_enrolled",
    "manual_review_required",
]

_TOKEN_TYPE = "referral_credential_enrollment"
_TOKEN_ISSUER = "dotmac_sub.auth.customer_credential_enrollment"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(hours=24)
_TOKEN_CLOCK_SKEW = timedelta(minutes=5)
_DELIVERY_LIMIT = 3
_DELIVERY_WINDOW_SECONDS = 15 * 60


class CustomerCredentialEnrollmentError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class EnrollmentContext:
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID
    email_digest: str


@dataclass(frozen=True)
class EnrollmentDeliveryResult:
    subscriber_id: UUID
    status: EnrollmentDeliveryStatus
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class EnrollmentCompletionResult:
    subscriber_id: UUID
    username: str
    email_verified: bool
    enrolled_at: datetime


def _email_digest(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_context(
    db: Session,
    *,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
    subscriber_id: UUID,
    lock: bool,
) -> tuple[Referral, Subscriber, EnrollmentContext]:
    referral_stmt = select(Referral).where(Referral.id == referral_id)
    subscriber_stmt = select(Subscriber).where(Subscriber.id == subscriber_id)
    lead_stmt = select(Lead).where(Lead.id == referred_lead_id)
    if lock:
        referral_stmt = referral_stmt.with_for_update()
        subscriber_stmt = subscriber_stmt.with_for_update()
        lead_stmt = lead_stmt.with_for_update()
    referral = db.scalars(referral_stmt).one_or_none()
    subscriber = db.scalars(subscriber_stmt).one_or_none()
    lead = db.scalars(lead_stmt).one_or_none()
    if referral is None or not referral.is_active or subscriber is None or lead is None:
        raise CustomerCredentialEnrollmentError(
            "Credential enrollment context was not found", status_code=404
        )
    if (
        referral.referred_party_id != referred_party_id
        or referral.referred_lead_id != referred_lead_id
        or referral.referred_subscriber_id != subscriber_id
        or subscriber.party_id != referred_party_id
        or lead.party_id != referred_party_id
        or lead.subscriber_id != subscriber_id
    ):
        raise CustomerCredentialEnrollmentError(
            "Credential enrollment context is stale or does not match"
        )
    if not subscriber.is_active or subscriber.status in {
        SubscriberStatus.canceled,
        SubscriberStatus.disabled,
    }:
        raise CustomerCredentialEnrollmentError(
            "Inactive, disabled, or canceled accounts cannot enroll a credential"
        )
    context = EnrollmentContext(
        referral_id=referral.id,
        referred_party_id=referred_party_id,
        referred_lead_id=referred_lead_id,
        subscriber_id=subscriber.id,
        email_digest=_email_digest(subscriber.email),
    )
    return referral, subscriber, context


def _local_credential(db: Session, subscriber_id: UUID) -> UserCredential | None:
    return db.scalars(
        select(UserCredential)
        .where(UserCredential.subscriber_id == subscriber_id)
        .where(UserCredential.provider == AuthProvider.local)
        .order_by(UserCredential.created_at.desc())
    ).first()


def _record_request_outcome(
    db: Session,
    *,
    subscriber_id: UUID,
    referral_id: UUID,
    delivery_status: EnrollmentDeliveryStatus,
) -> None:
    record_audit_event(
        db,
        action="auth.customer_credential_enrollment_requested",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_type=AuditActorType.system,
        metadata={
            "delivery_status": delivery_status,
            "referral_id": str(referral_id),
        },
        defer_until_commit=True,
    )
    db.commit()


def _issue_token(
    db: Session,
    context: EnrollmentContext,
    *,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    expires_at = issued_at + _TOKEN_TTL
    token = context_signing.sign_context_token(
        db,
        {
            "typ": _TOKEN_TYPE,
            "iss": _TOKEN_ISSUER,
            "ver": _TOKEN_VERSION,
            "sub": str(context.subscriber_id),
            "referral_id": str(context.referral_id),
            "referred_party_id": str(context.referred_party_id),
            "referred_lead_id": str(context.referred_lead_id),
            "subscriber_id": str(context.subscriber_id),
            "email_sha256": context.email_digest,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        },
    )
    return token, expires_at


def _decode_token(
    db: Session,
    token: str,
    *,
    now: datetime | None = None,
) -> EnrollmentContext:
    normalized_token = str(token or "").strip()
    if not normalized_token or len(normalized_token) > 4096:
        raise CustomerCredentialEnrollmentError(
            "Invalid credential enrollment token", status_code=401
        )
    try:
        payload = context_signing.verify_context_token(db, normalized_token)
    except JWTError as exc:
        raise CustomerCredentialEnrollmentError(
            "Invalid or expired credential enrollment token", status_code=401
        ) from exc
    if (
        payload.get("typ") != _TOKEN_TYPE
        or payload.get("iss") != _TOKEN_ISSUER
        or payload.get("ver") != _TOKEN_VERSION
    ):
        raise CustomerCredentialEnrollmentError(
            "Invalid credential enrollment token", status_code=401
        )
    try:
        context = EnrollmentContext(
            referral_id=UUID(str(payload["referral_id"])),
            referred_party_id=UUID(str(payload["referred_party_id"])),
            referred_lead_id=UUID(str(payload["referred_lead_id"])),
            subscriber_id=UUID(str(payload["subscriber_id"])),
            email_digest=str(payload["email_sha256"]),
        )
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CustomerCredentialEnrollmentError(
            "Invalid credential enrollment token", status_code=401
        ) from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if (
        str(payload.get("sub") or "") != str(context.subscriber_id)
        or len(context.email_digest) != 64
        or context.email_digest != context.email_digest.lower()
        or any(char not in string.hexdigits.lower() for char in context.email_digest)
        or issued_at > current + _TOKEN_CLOCK_SKEW
        or expires_at <= current
        or expires_at <= issued_at
        or expires_at - issued_at > _TOKEN_TTL + _TOKEN_CLOCK_SKEW
    ):
        raise CustomerCredentialEnrollmentError(
            "Invalid or expired credential enrollment token", status_code=401
        )
    return context


def request_referral_enrollment(
    db: Session,
    *,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
    subscriber_id: UUID,
) -> EnrollmentDeliveryResult:
    """Queue capability delivery without creating a token or placeholder password."""

    _, subscriber, context = _canonical_context(
        db,
        referral_id=referral_id,
        referred_party_id=referred_party_id,
        referred_lead_id=referred_lead_id,
        subscriber_id=subscriber_id,
        lock=False,
    )
    credential = _local_credential(db, subscriber.id)
    if credential is not None:
        state: EnrollmentDeliveryStatus = (
            "already_enrolled" if credential.is_active else "manual_review_required"
        )
        _record_request_outcome(
            db,
            subscriber_id=subscriber.id,
            referral_id=referral_id,
            delivery_status=state,
        )
        return EnrollmentDeliveryResult(subscriber_id=subscriber.id, status=state)

    decision = allow_operation(
        f"auth:referral-enrollment:{subscriber.id}",
        limit=_DELIVERY_LIMIT,
        window_seconds=_DELIVERY_WINDOW_SECONDS,
    )
    if not decision.allowed:
        _record_request_outcome(
            db,
            subscriber_id=subscriber.id,
            referral_id=referral_id,
            delivery_status="rate_limited",
        )
        return EnrollmentDeliveryResult(
            subscriber_id=subscriber.id,
            status="rate_limited",
            retry_after_seconds=decision.retry_after_seconds,
        )

    action_context: dict[str, object] = {
        "referral_id": str(context.referral_id),
        "referred_party_id": str(context.referred_party_id),
        "referred_lead_id": str(context.referred_lead_id),
        "subscriber_id": str(context.subscriber_id),
        "email_sha256": context.email_digest,
    }
    intent_result = submit_communication_intent(
        db,
        CommunicationIntent(
            subscriber_id=subscriber.id,
            event_type="auth.referral_credential_enrollment",
            category="credentials",
            subject="Complete your portal access",
            body=None,
            communication_class=CommunicationClass.transactional,
            channels=(NotificationChannel.email,),
            include_reseller=False,
            persist_policy_suppressions=True,
            subscriber_recipients={
                NotificationChannel.email: subscriber.email,
            },
            metadata={
                EPHEMERAL_ACTION_METADATA_KEY: ephemeral_action_descriptor(
                    action_type=REFERRAL_CREDENTIAL_ENROLLMENT_ACTION,
                    version=1,
                    context=action_context,
                )
            },
        ),
    )
    status: EnrollmentDeliveryStatus = (
        "queued" if intent_result.queued else "suppressed"
    )
    _record_request_outcome(
        db,
        subscriber_id=subscriber.id,
        referral_id=referral_id,
        delivery_status=status,
    )
    return EnrollmentDeliveryResult(
        subscriber_id=subscriber.id,
        status=status,
    )


def _context_from_delivery_metadata(context: dict[str, object]) -> EnrollmentContext:
    if set(context) != {
        "referral_id",
        "referred_party_id",
        "referred_lead_id",
        "subscriber_id",
        "email_sha256",
    }:
        raise EphemeralActionRejected("invalid_context")
    try:
        resolved = EnrollmentContext(
            referral_id=UUID(str(context["referral_id"])),
            referred_party_id=UUID(str(context["referred_party_id"])),
            referred_lead_id=UUID(str(context["referred_lead_id"])),
            subscriber_id=UUID(str(context["subscriber_id"])),
            email_digest=str(context["email_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    if (
        len(resolved.email_digest) != 64
        or resolved.email_digest != resolved.email_digest.lower()
        or any(char not in string.hexdigits.lower() for char in resolved.email_digest)
    ):
        raise EphemeralActionRejected("invalid_context")
    return resolved


def materialize_enrollment_email(
    db: Session,
    *,
    notification: Notification,
    context: dict[str, object],
) -> EphemeralEmailContent:
    """Mint and render the enrollment capability at the transport boundary."""

    requested = _context_from_delivery_metadata(context)
    if (
        notification.subscriber_id != requested.subscriber_id
        or _email_digest(notification.recipient) != requested.email_digest
    ):
        raise EphemeralActionRejected("recipient_context_mismatch")
    try:
        _, subscriber, canonical = _canonical_context(
            db,
            referral_id=requested.referral_id,
            referred_party_id=requested.referred_party_id,
            referred_lead_id=requested.referred_lead_id,
            subscriber_id=requested.subscriber_id,
            lock=False,
        )
    except CustomerCredentialEnrollmentError as exc:
        raise EphemeralActionRejected("stale_account_context") from exc
    if canonical != requested:
        raise EphemeralActionRejected("stale_account_context")
    if _local_credential(db, subscriber.id) is not None:
        raise EphemeralActionRejected("already_enrolled")

    token, _ = _issue_token(db, canonical)
    rendered = email_service.render_user_invite_email(
        db,
        to_email=subscriber.email,
        reset_token=token,
        person_name=subscriber.display_name or subscriber.first_name,
        expires_minutes=int(_TOKEN_TTL.total_seconds() // 60),
        action_path="/portal/auth/credential-enrollment",
        # Fragments stay out of normal HTTP request and access logs. The
        # browser moves the capability into a CSRF-protected POST body.
        token_in_fragment=True,
    )
    return EphemeralEmailContent(
        subject=rendered.subject,
        body_html=rendered.body_html,
        body_text=rendered.body_text,
        activity="auth_user_invite",
    )


def complete_referral_enrollment(
    db: Session,
    *,
    token: str,
    new_password: str,
    username: str | None = None,
) -> EnrollmentCompletionResult:
    """Create the user-chosen local credential and verify account email once."""

    context = _decode_token(db, token)
    minimum = auth_flow_service.password_min_length(db)
    if len(new_password) < minimum or len(new_password) > 255:
        raise CustomerCredentialEnrollmentError(
            f"Password must be between {minimum} and 255 characters", status_code=400
        )
    normalized_username = ""
    enrolled_at = datetime.now(UTC)
    try:
        with db.begin_nested():
            _, subscriber, canonical = _canonical_context(
                db,
                referral_id=context.referral_id,
                referred_party_id=context.referred_party_id,
                referred_lead_id=context.referred_lead_id,
                subscriber_id=context.subscriber_id,
                lock=True,
            )
            if canonical.email_digest != context.email_digest:
                raise CustomerCredentialEnrollmentError(
                    "Credential enrollment email has changed", status_code=401
                )
            if _local_credential(db, subscriber.id) is not None:
                raise CustomerCredentialEnrollmentError(
                    "Credential enrollment token has already been used",
                    status_code=401,
                )
            normalized_username = str(username or subscriber.email).strip().lower()
            if (
                not normalized_username
                or len(normalized_username) > 150
                or any(char.isspace() for char in normalized_username)
            ):
                raise CustomerCredentialEnrollmentError(
                    "A whitespace-free username of at most 150 characters is required",
                    status_code=422,
                )
            collision = db.scalars(
                select(UserCredential.id)
                .where(UserCredential.provider == AuthProvider.local)
                .where(func.lower(UserCredential.username) == normalized_username)
                .limit(1)
            ).first()
            if collision is not None:
                raise CustomerCredentialEnrollmentError(
                    "That username is unavailable", status_code=409
                )
            enrolled_at = datetime.now(UTC)
            credential = UserCredential(
                subscriber_id=subscriber.id,
                provider=AuthProvider.local,
                username=normalized_username,
                password_hash=auth_flow_service.hash_password(new_password),
                must_change_password=False,
                password_updated_at=enrolled_at,
                is_active=True,
            )
            db.add(credential)
            subscriber.email_verified = True
            stage_audit_event(
                db,
                action="auth.customer_credential_enrollment_completed",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                actor_type=AuditActorType.system,
                metadata={"referral_id": str(context.referral_id)},
            )
            db.flush()
        db.commit()
    except IntegrityError as exc:
        raise CustomerCredentialEnrollmentError(
            "That username is unavailable", status_code=409
        ) from exc
    auth_cache.invalidate_principal("subscriber", str(context.subscriber_id))
    return EnrollmentCompletionResult(
        subscriber_id=context.subscriber_id,
        username=normalized_username,
        email_verified=True,
        enrolled_at=enrolled_at,
    )
