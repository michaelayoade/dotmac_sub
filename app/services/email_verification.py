"""Exact Subscriber verification request owner and worker-only materializer.

The profile coordinator calls the flush-only participant in its existing owner
transaction. Public requests require a fresh adapter session. Legacy mobile
profile mutation remains outside this bounded cutover.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.notification import NotificationDeliveryLatency
from app.services import communication_intents
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.capability_recipient import resolve_capability_recipient
from app.services.domain_errors import DomainError
from app.services.ephemeral_communication_actions import (
    EMAIL_VERIFICATION_ACTION,
    EPHEMERAL_ACTION_METADATA_KEY,
    EphemeralActionRejected,
    EphemeralEmailContent,
    descriptor,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)

EMAIL_VERIFICATION_SCOPE = "auth:email_verification"
_DEFINITION = OwnerCommandDefinition(
    owner="auth.email_verification",
    concern="email verification delivery request",
    name="request_email_verification",
)


class EmailVerificationError(DomainError):
    """Stable request refusal, without submitted identity or capability values."""


@dataclass(frozen=True, slots=True)
class RequestEmailVerificationCommand:
    context: CommandContext
    subscriber_id: UUID
    actor: AuditActor


@dataclass(frozen=True, slots=True)
class EmailVerificationOutcome:
    intent_id: UUID | None
    queued: bool
    replayed: bool = False


def _digest(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _subscriber(db: Session, subscriber_id: UUID, *, lock: bool) -> Subscriber | None:
    statement = select(Subscriber).where(Subscriber.id == subscriber_id)
    if lock:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
        statement = statement.with_for_update()
    return db.execute(
        statement.execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _eligible(subscriber: Subscriber | None) -> bool:
    return bool(
        subscriber
        and subscriber.is_active
        and subscriber.email
        and not subscriber.email_verified
        and subscriber.status
        not in {SubscriberStatus.disabled, SubscriberStatus.canceled}
    )


def stage_email_verification(
    db: Session, *, command: RequestEmailVerificationCommand
) -> EmailVerificationOutcome:
    """Flush-only participant of this owner or customer.portal_profile_commands.

    The caller owns completion; failures propagate so profile and delivery
    evidence roll back together. Never dispatches or mints a bearer.
    """
    from app.services import auth_flow

    if not isinstance(command.subscriber_id, UUID):
        raise EmailVerificationError(
            code="auth.email_verification.invalid_command",
            message="A canonical Subscriber identifier is required.",
        )
    if not any(
        owner_command_active(db, owner=owner)
        for owner in ("auth.email_verification", "customer.portal_profile_commands")
    ):
        raise EmailVerificationError(
            code="auth.email_verification.invalid_participant",
            message="An authorized owner command is required.",
        )
    # Flush the coordinator's new email before reloading under the exact lock.
    db.flush()
    subscriber = _subscriber(db, command.subscriber_id, lock=True)
    if subscriber is None or not subscriber.email:
        return EmailVerificationOutcome(None, False)
    recipient = resolve_capability_recipient(
        subscriber.email, subject=f"subscriber:{subscriber.id}"
    )
    ttl = min(max(auth_flow._email_verification_ttl_minutes(db), 1), 1440)
    context: dict[str, object] = {
        "subscriber_id": str(subscriber.id),
        "email_sha256": _digest(recipient),
        "ttl_minutes": ttl,
        "purpose": "email_verification",
    }
    fingerprint = hashlib.sha256(
        f"{subscriber.id}:{context['email_sha256']}:{ttl}:email_verification:v1".encode()
    ).hexdigest()
    key = (
        "auth.email_verification:"
        + hashlib.sha256(
            str(command.context.idempotency_key or command.context.command_id).encode()
        ).hexdigest()
    )
    existing = db.execute(
        select(CommunicationIntentRecord).where(
            CommunicationIntentRecord.dedupe_key == key
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.event_type != EMAIL_VERIFICATION_ACTION
            or dict(existing.metadata_ or {}).get("request_fingerprint") != fingerprint
        ):
            raise EmailVerificationError(
                code="auth.email_verification.idempotency_conflict",
                message="Request key was already used for different verification evidence.",
            )
    elif not _eligible(subscriber):
        return EmailVerificationOutcome(None, False)
    result = communication_intents.submit(
        db,
        communication_intents.CommunicationIntent(
            subscriber_id=subscriber.id,
            event_type=EMAIL_VERIFICATION_ACTION,
            category="account",
            subject=None,
            body=None,
            channels=(NotificationChannel.email,),
            include_reseller=False,
            recipients={NotificationChannel.email: recipient},
            audience_type="subscriber",
            audience_id=subscriber.id,
            resolve_subscriber_identity=False,
            delivery_latency=NotificationDeliveryLatency.immediate,
            dedupe_key=key,
            metadata={
                "request_fingerprint": fingerprint,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                EPHEMERAL_ACTION_METADATA_KEY: descriptor(
                    action_type=EMAIL_VERIFICATION_ACTION, version=1, context=context
                ),
            },
        ),
    )
    # The shared planner performs its own dedupe read. A different-Subscriber
    # request can commit between our fingerprint read and that read.
    persisted = db.get(CommunicationIntentRecord, result.intent_id)
    if (
        persisted is None
        or dict(persisted.metadata_ or {}).get("request_fingerprint") != fingerprint
    ):
        raise EmailVerificationError(
            code="auth.email_verification.idempotency_conflict",
            message="Request key was already used for different verification evidence.",
        )
    if not result.replayed:
        stage_audit_event(
            db,
            actor=command.actor,
            action="auth.email_verification_requested",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            metadata={
                "intent_id": str(result.intent_id),
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "email_sha256": context["email_sha256"],
                "queued": bool(result.queued),
            },
        )
    db.flush()
    return EmailVerificationOutcome(
        result.intent_id, bool(result.queued), result.replayed
    )


def request_email_verification(
    db: Session, *, command: RequestEmailVerificationCommand
) -> EmailVerificationOutcome:
    if command.context.scope != EMAIL_VERIFICATION_SCOPE:
        raise EmailVerificationError(
            code="auth.email_verification.invalid_scope",
            message="Email verification scope is required.",
        )
    # This owner must never discard an adapter's pending or already-flushed
    # work. The shared executor's legacy precondition cleanup rolls it back.
    if db.in_transaction():
        raise EmailVerificationError(
            code="auth.email_verification.active_caller_transaction",
            message="Email verification requires a transaction-free session.",
        )
    try:
        return execute_owner_command(
            db,
            definition=_DEFINITION,
            context=command.context,
            operation=lambda: stage_email_verification(db, command=command),
        )
    except IntegrityError as exc:
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            != "uq_communication_intents_dedupe_key"
        ):
            raise
        raise EmailVerificationError(
            code="auth.email_verification.idempotency_conflict",
            message="Request key was already used for different verification evidence.",
            retryable=False,
        ) from exc
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise EmailVerificationError(
            code="auth.email_verification.lock_timeout",
            message="Verification delivery is temporarily unavailable. Please try again.",
            retryable=True,
        ) from exc


def materialize_verification_email(
    db: Session, *, notification: Notification, context: dict[str, object]
) -> EphemeralEmailContent:
    """Validate exact audience/current address before minting in worker memory."""
    from app.services import auth_flow, email
    from app.services.capability_recipient import CapabilityRecipientError

    if set(context) != {"subscriber_id", "email_sha256", "ttl_minutes", "purpose"}:
        raise EphemeralActionRejected("invalid_context")
    try:
        subscriber_id = UUID(str(context["subscriber_id"]))
    except (ValueError, TypeError) as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    digest = context["email_sha256"]
    ttl = context["ttl_minutes"]
    if (
        context["purpose"] != "email_verification"
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
        or type(ttl) is not int
        or not 1 <= ttl <= 1440
    ):
        raise EphemeralActionRejected("invalid_context")
    if (
        notification.audience_type != "subscriber"
        or notification.audience_id != subscriber_id
        or notification.subscriber_id != subscriber_id
        or _digest(notification.recipient or "") != digest
    ):
        raise EphemeralActionRejected("recipient_context_mismatch")
    subscriber = _subscriber(db, subscriber_id, lock=False)
    if not _eligible(subscriber) or subscriber is None:
        raise EphemeralActionRejected("stale_account_context")
    try:
        recipient = resolve_capability_recipient(
            subscriber.email, subject=f"subscriber:{subscriber_id}"
        )
    except CapabilityRecipientError as exc:
        raise EphemeralActionRejected("stale_account_context") from exc
    if _digest(recipient) != digest:
        raise EphemeralActionRejected("stale_account_context")
    token = auth_flow._issue_email_verification_token(
        db, str(subscriber_id), recipient, ttl_minutes=ttl
    )
    rendered = email.render_email_verification_email(
        db,
        to_email=recipient,
        verification_token=token,
        person_name=subscriber.display_name or subscriber.first_name,
        expires_minutes=ttl,
    )
    return EphemeralEmailContent(
        subject=rendered.subject,
        body_html=rendered.body_html,
        body_text=rendered.body_text,
        activity="auth_email_verification",
    )
