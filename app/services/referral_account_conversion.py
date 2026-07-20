"""Atomic account conversion for one exact Party-first referral context.

The context is the existing PII-free ``Referral``/``Party``/``Lead`` UUID
triple. Adapters carry all three identifiers and this owner rejects stale or
tampered values before it creates or attaches an account. Contact values are
never consulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.referral_native import Referral, ReferralStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.referral import ReferralSelfServiceAccountCreate
from app.schemas.subscriber import SubscriberCreate
from app.services import context_signing
from app.services import party as party_service
from app.services import subscriber as subscriber_service
from app.services.party import PartyInvariantError
from app.services.referrals import referrals as referrals_service

ReferralAccountOutcome = Literal["created", "attached", "already_attached"]


class ReferralAccountConversionError(ValueError):
    """A reviewed conversion command did not match canonical referral state."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ReferralAccountContext:
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID


@dataclass(frozen=True)
class ReferralAccountConversionResult:
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID
    outcome: ReferralAccountOutcome


@dataclass(frozen=True)
class ReferralSignupContextToken:
    token: str
    expires_at: datetime


_CONVERTIBLE_STATUSES = {
    ReferralStatus.pending.value,
    ReferralStatus.expired.value,
    ReferralStatus.qualified.value,
}
_PUBLIC_CONTEXT_TYPE = "referral_signup_context"
_PUBLIC_CONTEXT_ISSUER = "dotmac_sub.referrals.account_conversion"
_PUBLIC_CONTEXT_VERSION = 1
_PUBLIC_CONTEXT_TTL = timedelta(hours=24)
_PUBLIC_CONTEXT_CLOCK_SKEW = timedelta(minutes=5)


def _required(value: str, field_name: str, *, max_length: int | None = None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ReferralAccountConversionError(
            f"{field_name} is required", status_code=422
        )
    if max_length is not None and len(normalized) > max_length:
        raise ReferralAccountConversionError(
            f"{field_name} must be at most {max_length} characters",
            status_code=422,
        )
    return normalized


def _context_from_referral(referral: Referral) -> ReferralAccountContext:
    if referral.referred_party_id is None or referral.referred_lead_id is None:
        raise ReferralAccountConversionError(
            "Referral does not have a complete Party-first conversion context"
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
        raise ReferralAccountConversionError("Referral not found", status_code=404)
    context = _context_from_referral(referral)
    if (
        referral.referred_subscriber_id is None
        and referral.status not in _CONVERTIBLE_STATUSES
    ):
        raise ReferralAccountConversionError(
            f"Referral in status '{referral.status}' cannot receive an account"
        )
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    expires_at = issued_at + _PUBLIC_CONTEXT_TTL
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

    normalized_token = _required(token, "conversion_token", max_length=4096)
    try:
        payload = context_signing.verify_context_token(db, normalized_token)
    except JWTError as exc:
        raise ReferralAccountConversionError(
            "Invalid or expired referral signup context", status_code=401
        ) from exc
    if (
        payload.get("typ") != _PUBLIC_CONTEXT_TYPE
        or payload.get("iss") != _PUBLIC_CONTEXT_ISSUER
        or payload.get("ver") != _PUBLIC_CONTEXT_VERSION
    ):
        raise ReferralAccountConversionError(
            "Invalid referral signup context", status_code=401
        )
    try:
        context = ReferralAccountContext(
            referral_id=UUID(str(payload["referral_id"])),
            referred_party_id=UUID(str(payload["referred_party_id"])),
            referred_lead_id=UUID(str(payload["referred_lead_id"])),
        )
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ReferralAccountConversionError(
            "Invalid referral signup context", status_code=401
        ) from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if (
        str(payload.get("sub") or "") != str(context.referral_id)
        or issued_at > current + _PUBLIC_CONTEXT_CLOCK_SKEW
        or expires_at <= current
        or expires_at <= issued_at
        or expires_at - issued_at > _PUBLIC_CONTEXT_TTL + _PUBLIC_CONTEXT_CLOCK_SKEW
    ):
        raise ReferralAccountConversionError(
            "Invalid or expired referral signup context", status_code=401
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
        raise ReferralAccountConversionError("Referral not found", status_code=404)
    if referral.referred_party_id is None or referral.referred_lead_id is None:
        raise ReferralAccountConversionError(
            "Referral does not have a complete Party-first conversion context"
        )
    context = ReferralAccountContext(
        referral_id=referral.id,
        referred_party_id=referral.referred_party_id,
        referred_lead_id=referral.referred_lead_id,
    )
    if context.referred_party_id != referred_party_id:
        raise ReferralAccountConversionError(
            "Referral Party context is stale or does not match"
        )
    if context.referred_lead_id != referred_lead_id:
        raise ReferralAccountConversionError(
            "Referral Lead context is stale or does not match"
        )
    if (
        referral.referred_subscriber_id is None
        and referral.status not in _CONVERTIBLE_STATUSES
    ):
        raise ReferralAccountConversionError(
            f"Referral in status '{referral.status}' cannot receive an account"
        )
    return referral, context


def _lock_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.scalars(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    ).one_or_none()
    if subscriber is None:
        raise ReferralAccountConversionError("Subscriber not found", status_code=404)
    return subscriber


def _result(
    context: ReferralAccountContext,
    subscriber: Subscriber,
    *,
    outcome: ReferralAccountOutcome,
) -> ReferralAccountConversionResult:
    return ReferralAccountConversionResult(
        referral_id=context.referral_id,
        referred_party_id=context.referred_party_id,
        referred_lead_id=context.referred_lead_id,
        subscriber_id=subscriber.id,
        outcome=outcome,
    )


def _translate_error(exc: Exception) -> ReferralAccountConversionError:
    if isinstance(exc, ReferralAccountConversionError):
        return exc
    if isinstance(exc, HTTPException):
        return ReferralAccountConversionError(
            str(exc.detail), status_code=exc.status_code
        )
    return ReferralAccountConversionError(str(exc))


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
    referrals_service.attach_subscriber(
        db,
        referral_id=str(referral.id),
        subscriber_id=str(subscriber.id),
        source=source,
        reason=reason,
    )


def attach_existing_account(
    db: Session,
    *,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
    subscriber_id: UUID,
    source: str,
    reason: str,
) -> ReferralAccountConversionResult:
    """Adjudicate an existing account against one exact referral Party.

    The Subscriber may be unbound or already bound to the exact Party. A
    different Party is never repointed. This command commits the combined
    Party, Lead, and Referral evidence atomically and is idempotent for an
    exact retry.
    """

    normalized_source = _required(source, "source", max_length=80)
    normalized_reason = _required(reason, "reason")
    outcome: ReferralAccountOutcome
    try:
        with db.begin_nested():
            referral, context = _lock_context(
                db,
                referral_id=referral_id,
                referred_party_id=referred_party_id,
                referred_lead_id=referred_lead_id,
            )
            if referral.referred_subscriber_id is not None:
                if referral.referred_subscriber_id != subscriber_id:
                    raise ReferralAccountConversionError(
                        "Referral is already attached to a different Subscriber"
                    )
                subscriber = _lock_subscriber(db, subscriber_id)
                _bind_and_attach(
                    db,
                    referral=referral,
                    context=context,
                    subscriber=subscriber,
                    source=normalized_source,
                    reason=normalized_reason,
                )
                outcome = "already_attached"
            else:
                subscriber = _lock_subscriber(db, subscriber_id)
                _bind_and_attach(
                    db,
                    referral=referral,
                    context=context,
                    subscriber=subscriber,
                    source=normalized_source,
                    reason=normalized_reason,
                )
                outcome = "attached"
        db.commit()
        return _result(context, subscriber, outcome=outcome)
    except (PartyInvariantError, HTTPException, ValueError) as exc:
        raise _translate_error(exc) from exc


def create_account(
    db: Session,
    *,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
    subscriber_payload: SubscriberCreate,
    source: str,
    reason: str,
) -> ReferralAccountConversionResult:
    """Create and bind one account from an exact stable referral context.

    Subscriber compatibility fields come only from the explicit signup/admin
    payload. They are never copied from Party contact points or used to decide
    identity. An exact replay returns the account already attached to the
    referral instead of creating another Subscriber.
    """

    normalized_source = _required(source, "source", max_length=80)
    normalized_reason = _required(reason, "reason")
    outcome: ReferralAccountOutcome
    if subscriber_payload.person_id is not None:
        raise ReferralAccountConversionError(
            "Referral account creation cannot target an existing Subscriber; "
            "use attach_existing_account",
            status_code=422,
        )
    try:
        with db.begin_nested():
            referral, context = _lock_context(
                db,
                referral_id=referral_id,
                referred_party_id=referred_party_id,
                referred_lead_id=referred_lead_id,
            )
            if referral.referred_subscriber_id is not None:
                subscriber = _lock_subscriber(db, referral.referred_subscriber_id)
                if subscriber.party_id != context.referred_party_id:
                    raise ReferralAccountConversionError(
                        "Attached Subscriber does not match the referral Party"
                    )
                outcome = "already_attached"
            else:
                subscriber = subscriber_service.subscribers.prepare_new_account(
                    db, subscriber_payload
                )
                _bind_and_attach(
                    db,
                    referral=referral,
                    context=context,
                    subscriber=subscriber,
                    source=normalized_source,
                    reason=normalized_reason,
                )
                outcome = "created"
        if outcome == "created":
            subscriber_service.subscribers.commit_prepared_account(db, subscriber)
        else:
            db.commit()
        return _result(context, subscriber, outcome=outcome)
    except (PartyInvariantError, HTTPException, ValueError) as exc:
        raise _translate_error(exc) from exc


def create_public_account(
    db: Session,
    *,
    conversion_token: str,
    account_payload: ReferralSelfServiceAccountCreate,
) -> ReferralAccountConversionResult:
    """Create a new account from the signed public referral handoff.

    The token supplies identity continuity. The narrow public payload supplies
    account compatibility fields only and cannot choose lifecycle, reseller,
    billing, verification, numbering, or authorization state. Contact values
    are not compared with capture observations and never select identity.
    """

    context = decode_public_signup_context(db, conversion_token)
    subscriber_payload = SubscriberCreate(
        **account_payload.model_dump(),
        status=SubscriberStatus.new,
    )
    return create_account(
        db,
        referral_id=context.referral_id,
        referred_party_id=context.referred_party_id,
        referred_lead_id=context.referred_lead_id,
        subscriber_payload=subscriber_payload,
        source="public_referral_signup",
        reason="Public signup presented the signed Referral, Party, and Lead context",
    )
