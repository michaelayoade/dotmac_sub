"""Contracted account conversion for one exact Party-first referral context.

The PII-free ``Referral``/``Party``/``Lead`` UUID triple is the stable
conversion context. Public and staff adapters submit typed commands; this
coordinator revalidates canonical rows, calls each record owner through
transaction-neutral collaborators, and commits audit and event evidence once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar
from uuid import UUID

from jose import JWTError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.party import Party
from app.models.referral_native import Referral, ReferralStatus
from app.models.sales import Lead
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.referral import ReferralSelfServiceAccountCreate
from app.schemas.subscriber import SubscriberCreate
from app.services import context_signing
from app.services import party as party_service
from app.services import subscriber as subscriber_service
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.party import PartyInvariantError
from app.services.referrals import ReferralAttachmentError
from app.services.referrals import referrals as referrals_service
from app.services.settings_spec import resolve_value
from app.services.subscriber import SubscriberAccountPreparationError

ReferralAccountOutcome = Literal["created", "attached", "already_attached"]
ResultT = TypeVar("ResultT")

REFERRAL_ACCOUNT_CONVERSION_SCOPE = "referrals:account_conversion"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner="referrals.account_conversion",
    concern="atomic referral account creation and adjudication orchestration",
    name="create_referral_account",
)
_PUBLIC_CREATE_COMMAND = OwnerCommandDefinition(
    owner="referrals.account_conversion",
    concern="atomic referral account creation and adjudication orchestration",
    name="create_public_referral_account",
)
_ATTACH_COMMAND = OwnerCommandDefinition(
    owner="referrals.account_conversion",
    concern="atomic referral account creation and adjudication orchestration",
    name="attach_existing_referral_account",
)

_CONVERTIBLE_STATUSES = {
    ReferralStatus.pending.value,
    ReferralStatus.expired.value,
    ReferralStatus.qualified.value,
}
_PUBLIC_CONTEXT_TYPE = "referral_signup_context"
_PUBLIC_CONTEXT_ISSUER = "dotmac_sub.referrals.account_conversion"
_PUBLIC_CONTEXT_VERSION = 1
_PUBLIC_CONTEXT_CLOCK_SKEW = timedelta(minutes=5)
_PUBLIC_CONTEXT_MAX_TOKEN_LENGTH = 4096


class ReferralAccountConversionError(DomainError):
    """Stable, transport-neutral referral account conversion failure."""


def _error(
    code: str, message: str, **details: object
) -> ReferralAccountConversionError:
    return ReferralAccountConversionError(
        code=f"referrals.account_conversion.{code}",
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class ReferralAccountContext:
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID


@dataclass(frozen=True)
class CreateReferralAccountCommand:
    context: CommandContext
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_payload: SubscriberCreate


@dataclass(frozen=True)
class CreatePublicReferralAccountCommand:
    context: CommandContext
    conversion_token: str
    account_payload: ReferralSelfServiceAccountCreate


@dataclass(frozen=True)
class AttachExistingReferralAccountCommand:
    context: CommandContext
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID


@dataclass(frozen=True)
class ReferralAccountConversionResult:
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID
    outcome: ReferralAccountOutcome
    command_id: UUID | None = None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class ReferralSignupContextToken:
    token: str
    expires_at: datetime


def _required(value: str, field_name: str, *, max_length: int | None = None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise _error("invalid_command", f"{field_name} is required", field=field_name)
    if max_length is not None and len(normalized) > max_length:
        raise _error(
            "invalid_command",
            f"{field_name} must be at most {max_length} characters",
            field=field_name,
        )
    return normalized


def _validate_command_context(context: CommandContext) -> tuple[str, str]:
    if context.scope != REFERRAL_ACCOUNT_CONVERSION_SCOPE:
        raise _error(
            "invalid_command",
            "Referral account conversion command scope is invalid.",
            field="scope",
        )
    source = _required(context.actor, "actor", max_length=80)
    reason = _required(context.reason, "reason")
    return source, reason


def _policy_integer(db: Session, key: str) -> int:
    value = resolve_value(db, SettingDomain.subscriber, key)
    try:
        resolved = int(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(
            "invalid_configuration",
            "Referral signup capability policy is invalid.",
            setting=f"subscriber.{key}",
        ) from exc
    if resolved <= 0:
        raise _error(
            "invalid_configuration",
            "Referral signup capability policy is invalid.",
            setting=f"subscriber.{key}",
        )
    return resolved


def _public_context_ttl(db: Session) -> timedelta:
    return timedelta(
        minutes=_policy_integer(db, "referral_signup_context_expiry_minutes")
    )


def _context_from_referral(referral: Referral) -> ReferralAccountContext:
    if referral.referred_party_id is None or referral.referred_lead_id is None:
        raise _error(
            "incomplete_context",
            "Referral does not have a complete Party-first conversion context.",
        )
    return ReferralAccountContext(
        referral_id=referral.id,
        referred_party_id=referral.referred_party_id,
        referred_lead_id=referral.referred_lead_id,
    )


def issue_public_signup_context(
    db: Session,
    referral: Referral,
    *,
    now: datetime | None = None,
) -> ReferralSignupContextToken:
    """Mint an expiring, PII-free capability for this exact referral context."""

    if not referral.is_active:
        raise _error("context_not_found", "Referral not found.")
    context = _context_from_referral(referral)
    if (
        referral.referred_subscriber_id is None
        and referral.status not in _CONVERTIBLE_STATUSES
    ):
        raise _error(
            "context_not_convertible",
            f"Referral in status '{referral.status}' cannot receive an account.",
        )
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    expires_at = issued_at + _public_context_ttl(db)
    payload = {
        "typ": _PUBLIC_CONTEXT_TYPE,
        "iss": _PUBLIC_CONTEXT_ISSUER,
        "ver": _PUBLIC_CONTEXT_VERSION,
        "sub": str(context.referral_id),
        "referral_id": str(context.referral_id),
        "referred_party_id": str(context.referred_party_id),
        "referred_lead_id": str(context.referred_lead_id),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = context_signing.sign_context_token(db, payload)
    return ReferralSignupContextToken(token=token, expires_at=expires_at)


def decode_public_signup_context(
    db: Session,
    token: str,
    *,
    now: datetime | None = None,
) -> ReferralAccountContext:
    """Verify the signed capability and return its exact UUID context."""

    normalized_token = _required(
        token,
        "conversion_token",
        max_length=_PUBLIC_CONTEXT_MAX_TOKEN_LENGTH,
    )
    try:
        payload = context_signing.verify_context_token(db, normalized_token)
    except JWTError as exc:
        raise _error(
            "invalid_capability",
            "Invalid or expired referral signup context.",
        ) from exc
    if (
        payload.get("typ") != _PUBLIC_CONTEXT_TYPE
        or payload.get("iss") != _PUBLIC_CONTEXT_ISSUER
        or payload.get("ver") != _PUBLIC_CONTEXT_VERSION
    ):
        raise _error("invalid_capability", "Invalid referral signup context.")
    try:
        context = ReferralAccountContext(
            referral_id=UUID(str(payload["referral_id"])),
            referred_party_id=UUID(str(payload["referred_party_id"])),
            referred_lead_id=UUID(str(payload["referred_lead_id"])),
        )
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise _error("invalid_capability", "Invalid referral signup context.") from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    maximum_ttl = _public_context_ttl(db)
    if (
        str(payload.get("sub") or "") != str(context.referral_id)
        or issued_at > current + _PUBLIC_CONTEXT_CLOCK_SKEW
        or expires_at <= current
        or expires_at <= issued_at
        or expires_at - issued_at > maximum_ttl + _PUBLIC_CONTEXT_CLOCK_SKEW
    ):
        raise _error(
            "invalid_capability",
            "Invalid or expired referral signup context.",
        )
    return context


def _lock_context(
    db: Session,
    *,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
) -> tuple[Referral, ReferralAccountContext]:
    referral = db.scalars(
        select(Referral).where(Referral.id == referral_id).with_for_update()
    ).one_or_none()
    if referral is None or not referral.is_active:
        raise _error("context_not_found", "Referral not found.")
    context = _context_from_referral(referral)
    if context.referred_party_id != referred_party_id:
        raise _error(
            "stale_context", "Referral Party context is stale or does not match."
        )
    if context.referred_lead_id != referred_lead_id:
        raise _error(
            "stale_context", "Referral Lead context is stale or does not match."
        )
    if not (
        referral.party_bound_at is not None
        and str(referral.party_binding_source or "").strip()
        and str(referral.party_binding_reason or "").strip()
    ):
        raise _error(
            "incomplete_context", "Referral has incomplete Party binding evidence."
        )
    party = db.scalars(
        select(Party).where(Party.id == referred_party_id).with_for_update()
    ).one_or_none()
    lead = db.scalars(
        select(Lead).where(Lead.id == referred_lead_id).with_for_update()
    ).one_or_none()
    if party is None or lead is None:
        raise _error("context_not_found", "Referral conversion context was not found.")
    if lead.party_id != party.id:
        raise _error(
            "stale_context", "Referral Lead and Party context no longer match."
        )
    if (
        referral.referred_subscriber_id is None
        and referral.status not in _CONVERTIBLE_STATUSES
    ):
        raise _error(
            "context_not_convertible",
            f"Referral in status '{referral.status}' cannot receive an account.",
        )
    return referral, context


def _lock_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.scalars(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    ).one_or_none()
    if subscriber is None:
        raise _error("subscriber_not_found", "Subscriber not found.")
    return subscriber


def _result(
    context: ReferralAccountContext,
    subscriber: Subscriber,
    *,
    outcome: ReferralAccountOutcome,
    command_context: CommandContext,
) -> ReferralAccountConversionResult:
    return ReferralAccountConversionResult(
        referral_id=context.referral_id,
        referred_party_id=context.referred_party_id,
        referred_lead_id=context.referred_lead_id,
        subscriber_id=subscriber.id,
        outcome=outcome,
        command_id=command_context.command_id,
        correlation_id=command_context.correlation_id,
    )


def _bind_and_attach(
    db: Session,
    *,
    referral: Referral,
    context: ReferralAccountContext,
    subscriber: Subscriber,
    source: str,
    reason: str,
) -> None:
    party_service.bind_subscriber_account(
        db,
        subscriber_id=subscriber.id,
        party_id=context.referred_party_id,
        source=source,
        reason=reason,
    )
    referrals_service.attach_subscriber_for_conversion(
        db,
        referral_id=str(referral.id),
        subscriber_id=str(subscriber.id),
        source=source,
        reason=reason,
    )


def _stage_conversion_evidence(
    db: Session,
    *,
    context: ReferralAccountContext,
    subscriber: Subscriber,
    outcome: Literal["created", "attached"],
    command_context: CommandContext,
) -> None:
    evidence = {
        "schema_version": 1,
        "referral_id": str(context.referral_id),
        "referred_party_id": str(context.referred_party_id),
        "referred_lead_id": str(context.referred_lead_id),
        "subscriber_id": str(subscriber.id),
        "outcome": outcome,
        "command_id": str(command_context.command_id),
        "correlation_id": str(command_context.correlation_id),
    }
    if command_context.causation_id is not None:
        evidence["causation_id"] = str(command_context.causation_id)
    stage_audit_event(
        db,
        action="referrals.account_converted",
        entity_type="referral",
        entity_id=str(context.referral_id),
        actor_type=AuditActorType.system,
        actor_id=command_context.actor,
        metadata={"owner": "referrals.account_conversion", **evidence},
    )
    emit_event(
        db,
        EventType.referral_account_converted,
        evidence,
        actor=command_context.actor,
        subscriber_id=subscriber.id,
    )


def _execute_conversion(
    db: Session,
    *,
    definition: OwnerCommandDefinition,
    context: CommandContext,
    operation: Callable[[], ResultT],
) -> ResultT:
    try:
        return execute_owner_command(
            db,
            definition=definition,
            context=context,
            operation=operation,
        )
    except SubscriberAccountPreparationError as exc:
        raise _error("invalid_command", str(exc)) from exc
    except ReferralAttachmentError as exc:
        allowed_codes = {
            "account_conflict",
            "context_not_found",
            "incomplete_context",
            "invalid_command",
            "self_referral",
            "subscriber_not_found",
        }
        code = exc.code if exc.code in allowed_codes else "account_conflict"
        raise _error(code, str(exc)) from exc
    except PartyInvariantError as exc:
        raise _error("account_conflict", str(exc)) from exc
    except IntegrityError as exc:
        raise _error(
            "account_conflict",
            "Referral account conversion conflicts with canonical account state.",
        ) from exc


def _create_operation(
    db: Session,
    *,
    command_context: CommandContext,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
    subscriber_payload: SubscriberCreate,
) -> ReferralAccountConversionResult:
    source, reason = _validate_command_context(command_context)
    if subscriber_payload.person_id is not None:
        raise _error(
            "invalid_command",
            "Referral account creation cannot target an existing Subscriber; "
            "use the attach-existing command.",
            field="person_id",
        )
    referral, context = _lock_context(
        db,
        referral_id=referral_id,
        referred_party_id=referred_party_id,
        referred_lead_id=referred_lead_id,
    )
    if referral.referred_subscriber_id is not None:
        subscriber = _lock_subscriber(db, referral.referred_subscriber_id)
        if subscriber.party_id != context.referred_party_id:
            raise _error(
                "stale_context",
                "Attached Subscriber does not match the referral Party.",
            )
        _bind_and_attach(
            db,
            referral=referral,
            context=context,
            subscriber=subscriber,
            source=source,
            reason=reason,
        )
        return _result(
            context,
            subscriber,
            outcome="already_attached",
            command_context=command_context,
        )

    subscriber = subscriber_service.subscribers.prepare_new_account(
        db, subscriber_payload
    )
    _bind_and_attach(
        db,
        referral=referral,
        context=context,
        subscriber=subscriber,
        source=source,
        reason=reason,
    )
    subscriber_service.subscribers.stage_prepared_account_created_event(
        db, subscriber, actor=command_context.actor
    )
    _stage_conversion_evidence(
        db,
        context=context,
        subscriber=subscriber,
        outcome="created",
        command_context=command_context,
    )
    return _result(
        context,
        subscriber,
        outcome="created",
        command_context=command_context,
    )


def create_account(
    db: Session,
    command: CreateReferralAccountCommand,
) -> ReferralAccountConversionResult:
    """Create and bind one account from exact stable referral context."""

    return _execute_conversion(
        db,
        definition=_CREATE_COMMAND,
        context=command.context,
        operation=lambda: _create_operation(
            db,
            command_context=command.context,
            referral_id=command.referral_id,
            referred_party_id=command.referred_party_id,
            referred_lead_id=command.referred_lead_id,
            subscriber_payload=command.subscriber_payload,
        ),
    )


def create_public_account(
    db: Session,
    command: CreatePublicReferralAccountCommand,
) -> ReferralAccountConversionResult:
    """Create the exact account represented by one signed public handoff."""

    def operation() -> ReferralAccountConversionResult:
        context = decode_public_signup_context(db, command.conversion_token)
        subscriber_payload = SubscriberCreate(
            **command.account_payload.model_dump(),
            status=SubscriberStatus.new,
        )
        return _create_operation(
            db,
            command_context=command.context,
            referral_id=context.referral_id,
            referred_party_id=context.referred_party_id,
            referred_lead_id=context.referred_lead_id,
            subscriber_payload=subscriber_payload,
        )

    return _execute_conversion(
        db,
        definition=_PUBLIC_CREATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def attach_existing_account(
    db: Session,
    command: AttachExistingReferralAccountCommand,
) -> ReferralAccountConversionResult:
    """Adjudicate an existing account against one exact referral Party."""

    def operation() -> ReferralAccountConversionResult:
        source, reason = _validate_command_context(command.context)
        referral, context = _lock_context(
            db,
            referral_id=command.referral_id,
            referred_party_id=command.referred_party_id,
            referred_lead_id=command.referred_lead_id,
        )
        if (
            referral.referred_subscriber_id is not None
            and referral.referred_subscriber_id != command.subscriber_id
        ):
            raise _error(
                "account_conflict",
                "Referral is already attached to a different Subscriber.",
            )
        subscriber = _lock_subscriber(db, command.subscriber_id)
        if (
            referral.referred_subscriber_id == subscriber.id
            and subscriber.party_id != context.referred_party_id
        ):
            raise _error(
                "stale_context",
                "Attached Subscriber does not match the referral Party.",
            )
        outcome: ReferralAccountOutcome = (
            "already_attached"
            if referral.referred_subscriber_id == subscriber.id
            else "attached"
        )
        _bind_and_attach(
            db,
            referral=referral,
            context=context,
            subscriber=subscriber,
            source=source,
            reason=reason,
        )
        if outcome == "attached":
            _stage_conversion_evidence(
                db,
                context=context,
                subscriber=subscriber,
                outcome="attached",
                command_context=command.context,
            )
        return _result(
            context,
            subscriber,
            outcome=outcome,
            command_context=command.context,
        )

    return _execute_conversion(
        db,
        definition=_ATTACH_COMMAND,
        context=command.context,
        operation=operation,
    )
