"""Canonical Refer & Earn program owner.

Public adapters submit typed commands for referral-code issuance, Party-first
capture, qualification, rejection, and reward issuance.  The owner locks the
canonical rows, composes transaction-neutral Party, Lead, and credit-note
collaborators, and stages PII-free audit/event evidence before one commit.

Contact observations are used only for conservative risk rejection and
same-code request deduplication.  They never select a Party or Subscriber.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.party import Party, PartyContactPoint, PartyContactPointType, PartyType
from app.models.referral_native import (
    Referral,
    ReferralCode,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import party as party_service
from app.services import settings_spec
from app.services.audit_adapter import stage_audit_event
from app.services.billing.credit_notes import (
    CreditNoteReferralRewardError,
    CreditNotes,
)
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.party import PartyInvariantError
from app.services.sales import lifecycle as lead_lifecycle

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT")
ReferralTransitionOutcome = Literal[
    "captured",
    "duplicate_capture",
    "qualified",
    "expired",
    "already_qualified",
    "not_applicable",
    "rejected",
    "already_rejected",
    "reward_issued",
    "reward_reconciled",
    "already_rewarded",
]

REFERRAL_PROGRAM_SCOPE = "referrals:program"

# Protocol/model invariants. Operator-tunable program policy lives only in the
# canonical settings specification and is resolved through ``settings_spec``.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_CODE_GENERATION_ATTEMPTS = 12
_REFERRAL_LEAD_SOURCE = "Referrer"
_DOMAIN = SettingDomain.subscriber
_PROGRAM_POLICY_KEYS = (
    "referral_program_enabled",
    "referral_reward_amount",
    "referral_reward_currency",
    "referral_qualify_window_days",
    "referral_auto_approve_reward",
)
_SHARE_BASE_URL_KEY = "referral_share_base_url"

_PROGRAM_TRANSITION_CONCERN = "atomic referral program transition orchestration"
_ENSURE_CODE_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="ensure_referral_code",
)
_CAPTURE_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="capture_referral",
)
_REFER_FRIEND_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="refer_friend",
)
_QUALIFY_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="qualify_referral_for_subscriber",
)
_QUALIFY_OVERRIDE_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="qualify_referral_override",
)
_REJECT_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="reject_referral",
)
_ISSUE_REWARD_COMMAND = OwnerCommandDefinition(
    owner="referrals.program",
    concern=_PROGRAM_TRANSITION_CONCERN,
    name="issue_referral_reward",
)


class ReferralProgramError(DomainError):
    """Stable, transport-neutral rejection from the referral program owner."""


class ReferralAttachmentError(ValueError):
    """Transport-neutral rejection from the nested attachment collaborator."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class EnsureReferralCodeCommand:
    context: CommandContext
    subscriber_id: UUID


@dataclass(frozen=True)
class CaptureReferralCommand:
    context: CommandContext
    code: str
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    region: str | None = None
    address: str | None = None
    notes: str | None = None
    source: str = "public"


@dataclass(frozen=True)
class ReferFriendCommand:
    context: CommandContext
    referrer_subscriber_id: UUID
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class QualifyReferralForSubscriberCommand:
    context: CommandContext
    subscriber_id: UUID


@dataclass(frozen=True)
class QualifyReferralOverrideCommand:
    context: CommandContext
    referral_id: UUID


@dataclass(frozen=True)
class RejectReferralCommand:
    context: CommandContext
    referral_id: UUID
    reason: str


@dataclass(frozen=True)
class IssueReferralRewardCommand:
    context: CommandContext
    referral_id: UUID


@dataclass(frozen=True)
class ReferralCodeOutcome:
    subscriber_id: UUID
    referral_code_id: UUID
    code: str
    outcome: Literal["issued", "already_issued"]
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class ReferralCaptureOutcome:
    referral_id: UUID
    referrer_subscriber_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    status: str
    outcome: Literal["captured", "duplicate_capture"]
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class ReferralTransitionResult:
    referral_id: UUID | None
    status: str | None
    reward_status: str | None
    outcome: ReferralTransitionOutcome
    credit_note_id: UUID | None
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class _ProgramPolicy:
    enabled: bool
    amount: Decimal
    currency: str
    window_days: int
    auto_approve: bool


@dataclass(frozen=True)
class _CaptureValues:
    code: str
    name: str | None
    email: str | None
    phone: str | None
    region: str | None
    address: str | None
    notes: str | None
    source: str


def _error(code: str, message: str, **details: object) -> ReferralProgramError:
    return ReferralProgramError(
        code=f"referrals.program.{code}",
        message=message,
        details=details,
    )


def _required(value: object, field: str, *, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise _error("invalid_command", f"{field} is required.", field=field)
    if len(normalized) > max_length:
        raise _error(
            "invalid_command",
            f"{field} exceeds the canonical record limit.",
            field=field,
        )
    return normalized


def _optional(value: object, field: str, *, max_length: int) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise _error(
            "invalid_command",
            f"{field} exceeds the canonical record limit.",
            field=field,
        )
    return normalized


def _validate_context(context: CommandContext) -> None:
    if context.scope != REFERRAL_PROGRAM_SCOPE:
        raise _error(
            "invalid_command",
            "Referral program command scope is invalid.",
            field="scope",
        )


def _policy_values(db: Session) -> dict[str, object]:
    values = settings_spec.resolve_values_atomic(
        db, _DOMAIN, list(_PROGRAM_POLICY_KEYS)
    )
    missing = sorted(set(_PROGRAM_POLICY_KEYS) - set(values))
    if missing:
        raise _error(
            "invalid_configuration",
            "Referral program policy is incomplete.",
            settings=[f"subscriber.{key}" for key in missing],
        )
    return values


def _program_policy(db: Session) -> _ProgramPolicy:
    values = _policy_values(db)
    enabled = values["referral_program_enabled"]
    auto_approve = values["referral_auto_approve_reward"]
    window_days = values["referral_qualify_window_days"]
    if not isinstance(enabled, bool) or not isinstance(auto_approve, bool):
        raise _error(
            "invalid_configuration", "Referral program boolean policy is invalid."
        )
    if isinstance(window_days, bool) or not isinstance(window_days, int):
        raise _error(
            "invalid_configuration", "Referral qualification policy is invalid."
        )
    try:
        amount = Decimal(str(values["referral_reward_amount"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "invalid_configuration", "Referral reward amount policy is invalid."
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise _error(
            "invalid_configuration", "Referral reward amount policy is invalid."
        )
    currency = str(values["referral_reward_currency"] or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _error(
            "invalid_configuration", "Referral reward currency policy is invalid."
        )
    return _ProgramPolicy(
        enabled=enabled,
        amount=amount,
        currency=currency,
        window_days=window_days,
        auto_approve=auto_approve,
    )


def _share_base_url(db: Session) -> str:
    value = settings_spec.resolve_value(db, _DOMAIN, _SHARE_BASE_URL_KEY)
    share_base_url = str(value or "").strip().rstrip("/")
    parsed_share_url = urlsplit(share_base_url)
    if parsed_share_url.scheme not in {"http", "https"} or not parsed_share_url.netloc:
        raise _error("invalid_configuration", "Referral share URL policy is invalid.")
    return share_base_url


def _normalize_capture(command: CaptureReferralCommand) -> _CaptureValues:
    code = _required(command.code, "code", max_length=24).upper()
    email = _optional(command.email, "email", max_length=255)
    phone = _optional(command.phone, "phone", max_length=40)
    if email is None and phone is None:
        raise _error(
            "contact_required",
            "An email or phone number is required to refer someone.",
        )
    return _CaptureValues(
        code=code,
        name=_optional(command.name, "name", max_length=160),
        email=email,
        phone=phone,
        region=_optional(command.region, "region", max_length=80),
        address=_optional(command.address, "address", max_length=500),
        notes=_optional(command.notes, "notes", max_length=1000),
        source=_required(command.source, "source", max_length=40),
    )


def _generate_code(db: Session) -> str:
    for _ in range(_CODE_GENERATION_ATTEMPTS):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if not db.scalar(select(ReferralCode.id).where(ReferralCode.code == code)):
            return code
    raise _error(
        "code_generation_exhausted",
        "Could not generate a unique referral code; retry the command.",
    )


def _subscriber_is_active(db: Session, subscriber: Subscriber) -> bool:
    if subscriber.status == SubscriberStatus.active:
        return True
    from app.models.catalog import Subscription, SubscriptionStatus

    return (
        db.scalar(
            select(Subscription.id)
            .where(Subscription.subscriber_id == subscriber.id)
            .where(Subscription.status == SubscriptionStatus.active)
            .limit(1)
        )
        is not None
    )


def _normalized_contact(
    db: Session, email: str | None, phone: str | None
) -> tuple[str | None, str | None]:
    country = default_country_code(db)
    return (
        normalize_email_identifier(email),
        normalize_phone_identifier(phone, default_country_code=country),
    )


def _resolved_subscribers_for_risk_guard(
    db: Session, *, email: str | None, phone: str | None
) -> list[Subscriber]:
    """Resolve exact account matches only for conservative rejection."""

    matches: dict[UUID, Subscriber] = {}
    for identifier, hint in ((email, "email"), (phone, "phone")):
        if not identifier:
            continue
        resolution = resolve_customer_identity(db, identifier, channel_hint=hint)
        if resolution.matched and resolution.subscriber_id is not None:
            subscriber = db.get(Subscriber, resolution.subscriber_id)
            if subscriber is not None:
                matches[subscriber.id] = subscriber
    return list(matches.values())


def _capture_meta(referral: Referral) -> dict[str, object]:
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    capture = meta.get("capture")
    return capture if isinstance(capture, dict) else {}


def _existing_referral_by_capture_contact(
    db: Session,
    *,
    referral_code_id: UUID,
    email: str | None,
    phone: str | None,
) -> Referral | None:
    """Recognize a same-code retry without treating contact as identity."""

    norm_email, norm_phone = _normalized_contact(db, email, phone)
    submitted: dict[str, str] = {}
    if norm_email:
        submitted[PartyContactPointType.email.value] = norm_email
    if norm_phone:
        submitted[PartyContactPointType.phone.value] = norm_phone
    if not submitted:
        return None
    candidates = db.scalars(
        select(Referral)
        .where(Referral.referral_code_id == referral_code_id)
        .where(Referral.is_active.is_(True))
        .order_by(Referral.created_at.desc())
    ).all()
    for referral in candidates:
        captured: dict[str, str] = {}
        if referral.referred_party_id is not None:
            points = db.scalars(
                select(PartyContactPoint)
                .where(PartyContactPoint.party_id == referral.referred_party_id)
                .where(PartyContactPoint.is_active.is_(True))
                .where(
                    PartyContactPoint.channel_type.in_(
                        (
                            PartyContactPointType.email.value,
                            PartyContactPointType.phone.value,
                        )
                    )
                )
            ).all()
            captured = {point.channel_type: point.normalized_value for point in points}
        else:
            capture = _capture_meta(referral)
            legacy_email, legacy_phone = _normalized_contact(
                db,
                str(capture.get("email") or "") or None,
                str(capture.get("phone") or "") or None,
            )
            if legacy_email:
                captured[PartyContactPointType.email.value] = legacy_email
            if legacy_phone:
                captured[PartyContactPointType.phone.value] = legacy_phone
        if captured == submitted:
            return referral
    return None


def _create_capture_party(
    db: Session, *, name: str | None, email: str | None, phone: str | None
) -> Party:
    display_name = name or "Referred prospect"
    party = party_service.create_party(
        db,
        party_type=PartyType.person,
        display_name=display_name,
        metadata={"created_by": "referrals.program"},
    )
    party_service.quarantine_party(
        db,
        party_id=party.id,
        reason="Public referral contact is unverified pending identity review",
    )
    normalized_email, normalized_phone = _normalized_contact(db, email, phone)
    for channel_type, normalized_value, display_value in (
        (PartyContactPointType.email, normalized_email, email),
        (PartyContactPointType.phone, normalized_phone, phone),
    ):
        if normalized_value is None:
            continue
        party_service.add_contact_point(
            db,
            party_id=party.id,
            channel_type=channel_type,
            normalized_value=normalized_value,
            display_value=display_value,
            is_primary=True,
            metadata={"observed_by": "referrals.program"},
        )
    return party


def _referred_display_name(referral: Referral) -> str | None:
    if referral.referred_party is not None:
        return referral.referred_party.display_name
    name = _capture_meta(referral).get("name")
    if name:
        return str(name)
    referred = referral.referred_subscriber
    if referred is not None:
        display = (
            referred.display_name
            or f"{referred.first_name} {referred.last_name}".strip()
        )
        return display or None
    return None


def _stage_code_evidence(
    db: Session,
    *,
    code: ReferralCode,
    context: CommandContext,
) -> None:
    evidence = {
        "schema_version": 1,
        "referral_code_id": str(code.id),
        "subscriber_id": str(code.subscriber_id),
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
    }
    stage_audit_event(
        db,
        action="referrals.code_issued",
        entity_type="referral_code",
        entity_id=str(code.id),
        actor_type=AuditActorType.system,
        actor_id=context.actor,
        metadata={"owner": "referrals.program", **evidence},
    )
    emit_event(
        db,
        EventType.referral_code_issued,
        evidence,
        actor=context.actor,
        subscriber_id=code.subscriber_id,
    )


def _stage_referral_evidence(
    db: Session,
    *,
    referral: Referral,
    event_type: EventType,
    action: str,
    outcome: str,
    context: CommandContext,
    extra: dict[str, object] | None = None,
) -> None:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "referral_id": str(referral.id),
        "referrer_subscriber_id": str(referral.referrer_subscriber_id),
        "referred_party_id": (
            str(referral.referred_party_id) if referral.referred_party_id else None
        ),
        "referred_subscriber_id": (
            str(referral.referred_subscriber_id)
            if referral.referred_subscriber_id
            else None
        ),
        "referred_lead_id": (
            str(referral.referred_lead_id) if referral.referred_lead_id else None
        ),
        "status": referral.status,
        "reward_status": referral.reward_status,
        "outcome": outcome,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
    }
    if context.causation_id is not None:
        evidence["causation_id"] = str(context.causation_id)
    if extra:
        evidence.update(extra)
    stage_audit_event(
        db,
        action=action,
        entity_type="referral",
        entity_id=str(referral.id),
        actor_type=AuditActorType.system,
        actor_id=context.actor,
        metadata={"owner": "referrals.program", **evidence},
    )
    emit_event(
        db,
        event_type,
        evidence,
        actor=context.actor,
        subscriber_id=(
            referral.referrer_subscriber_id
            if event_type
            in {
                EventType.referral_captured,
                EventType.referral_reward_issued,
                EventType.referral_reward_reconciled,
            }
            else referral.referred_subscriber_id
        ),
    )


def _transition_result(
    referral: Referral | None,
    *,
    outcome: ReferralTransitionOutcome,
    context: CommandContext,
    credit_note_id: UUID | None = None,
) -> ReferralTransitionResult:
    return ReferralTransitionResult(
        referral_id=referral.id if referral else None,
        status=referral.status if referral else None,
        reward_status=referral.reward_status if referral else None,
        outcome=outcome,
        credit_note_id=credit_note_id,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _execute(
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
    except ReferralProgramError:
        raise
    except ReferralAttachmentError as exc:
        raise _error(exc.code, str(exc)) from exc
    except (PartyInvariantError, lead_lifecycle.LeadLifecycleError) as exc:
        raise _error(
            "collaboration_conflict",
            "Referral identity or Lead state conflicts with canonical records.",
        ) from exc
    except CreditNoteReferralRewardError as exc:
        raise _error(exc.code, str(exc)) from exc
    except IntegrityError as exc:
        raise _error(
            "write_conflict",
            "Referral state changed concurrently; retry the command.",
        ) from exc


def _lock_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.scalars(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    ).one_or_none()
    if subscriber is None:
        raise _error("subscriber_not_found", "Subscriber not found.")
    return subscriber


def _lock_referral(db: Session, referral_id: UUID) -> Referral:
    referral = db.scalars(
        select(Referral).where(Referral.id == referral_id).with_for_update()
    ).one_or_none()
    if referral is None or not referral.is_active:
        raise _error("referral_not_found", "Referral not found.")
    return referral


def _ensure_code_operation(
    db: Session,
    *,
    subscriber_id: UUID,
    context: CommandContext,
) -> ReferralCodeOutcome:
    _lock_subscriber(db, subscriber_id)
    existing = db.scalars(
        select(ReferralCode)
        .where(ReferralCode.subscriber_id == subscriber_id)
        .where(ReferralCode.is_active.is_(True))
        .order_by(ReferralCode.created_at.desc())
        .limit(1)
    ).one_or_none()
    if existing is not None:
        return ReferralCodeOutcome(
            subscriber_id=subscriber_id,
            referral_code_id=existing.id,
            code=existing.code,
            outcome="already_issued",
            command_id=context.command_id,
            correlation_id=context.correlation_id,
        )
    code = ReferralCode(subscriber_id=subscriber_id, code=_generate_code(db))
    db.add(code)
    db.flush()
    _stage_code_evidence(db, code=code, context=context)
    return ReferralCodeOutcome(
        subscriber_id=subscriber_id,
        referral_code_id=code.id,
        code=code.code,
        outcome="issued",
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def ensure_referral_code(
    db: Session, command: EnsureReferralCodeCommand
) -> ReferralCodeOutcome:
    _validate_context(command.context)
    return _execute(
        db,
        definition=_ENSURE_CODE_COMMAND,
        context=command.context,
        operation=lambda: _ensure_code_operation(
            db,
            subscriber_id=command.subscriber_id,
            context=command.context,
        ),
    )


def _capture_operation(
    db: Session,
    *,
    command: CaptureReferralCommand,
) -> ReferralCaptureOutcome:
    values = _normalize_capture(command)
    policy = _program_policy(db)
    if not policy.enabled:
        raise _error("program_disabled", "Referral program is not enabled.")
    ref_code = db.scalars(
        select(ReferralCode)
        .where(ReferralCode.code == values.code)
        .where(ReferralCode.is_active.is_(True))
        .with_for_update()
    ).one_or_none()
    if ref_code is None:
        raise _error("code_not_found", "Invalid referral code.")

    for existing_subscriber in _resolved_subscribers_for_risk_guard(
        db, email=values.email, phone=values.phone
    ):
        if existing_subscriber.id == ref_code.subscriber_id:
            raise _error("self_referral", "A referrer cannot self-refer.")
        if (
            existing_subscriber.status == SubscriberStatus.active
            and existing_subscriber.is_active
        ):
            raise _error(
                "existing_customer",
                "The submitted contact belongs to an active customer.",
            )

    existing = _existing_referral_by_capture_contact(
        db,
        referral_code_id=ref_code.id,
        email=values.email,
        phone=values.phone,
    )
    if existing is not None:
        if existing.referred_party_id is None or existing.referred_lead_id is None:
            raise _error(
                "incomplete_context",
                "The existing referral has incomplete Party-first evidence.",
            )
        return ReferralCaptureOutcome(
            referral_id=existing.id,
            referrer_subscriber_id=existing.referrer_subscriber_id,
            referred_party_id=existing.referred_party_id,
            referred_lead_id=existing.referred_lead_id,
            status=existing.status,
            outcome="duplicate_capture",
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    referred_party = _create_capture_party(
        db,
        name=values.name,
        email=values.email,
        phone=values.phone,
    )
    lead = lead_lifecycle.create_party_lead(
        db,
        party_id=referred_party.id,
        title=f"Referral: {referred_party.display_name}",
        lead_source=_REFERRAL_LEAD_SOURCE,
        binding_source="referrals.program",
        binding_reason="Party created for this referral capture",
        origin_capture={
            "capture_method": "referral",
            "source_platform": "referral",
            "capture_source": values.source,
            "capture_reason": "Prospect submitted through a referral code",
        },
        region=values.region,
        address=values.address,
        notes=values.notes,
        metadata={
            "referral_code": ref_code.code,
            "referrer_subscriber_id": str(ref_code.subscriber_id),
        },
    )
    referral = Referral(
        referrer_subscriber_id=ref_code.subscriber_id,
        referral_code_id=ref_code.id,
        referred_party_id=referred_party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="referrals.program",
        party_binding_reason="Party created for this referral capture",
        referred_lead_id=lead.id,
        status=ReferralStatus.pending.value,
        reward_currency=policy.currency,
        source=values.source,
    )
    db.add(referral)
    db.flush()
    _stage_referral_evidence(
        db,
        referral=referral,
        event_type=EventType.referral_captured,
        action="referrals.captured",
        outcome="captured",
        context=command.context,
    )
    logger.info(
        "referral_captured",
        extra={
            "referral_id": str(referral.id),
            "referrer_subscriber_id": str(referral.referrer_subscriber_id),
            "referred_party_id": str(referred_party.id),
        },
    )
    return ReferralCaptureOutcome(
        referral_id=referral.id,
        referrer_subscriber_id=referral.referrer_subscriber_id,
        referred_party_id=referred_party.id,
        referred_lead_id=lead.id,
        status=referral.status,
        outcome="captured",
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
    )


def capture_referral(
    db: Session, command: CaptureReferralCommand
) -> ReferralCaptureOutcome:
    _validate_context(command.context)
    return _execute(
        db,
        definition=_CAPTURE_COMMAND,
        context=command.context,
        operation=lambda: _capture_operation(db, command=command),
    )


def refer_friend(db: Session, command: ReferFriendCommand) -> ReferralCaptureOutcome:
    _validate_context(command.context)

    def operation() -> ReferralCaptureOutcome:
        code = _ensure_code_operation(
            db,
            subscriber_id=command.referrer_subscriber_id,
            context=command.context,
        )
        return _capture_operation(
            db,
            command=CaptureReferralCommand(
                context=command.context,
                code=code.code,
                name=command.name,
                email=command.email,
                phone=command.phone,
                notes=command.note,
                source="portal",
            ),
        )

    return _execute(
        db,
        definition=_REFER_FRIEND_COMMAND,
        context=command.context,
        operation=operation,
    )


def _find_referral_for_subscriber(
    db: Session, subscriber_id: UUID, party_id: UUID | None
) -> UUID | None:
    referral_id = db.scalar(
        select(Referral.id)
        .where(Referral.referred_subscriber_id == subscriber_id)
        .where(Referral.is_active.is_(True))
        .order_by(Referral.created_at.asc())
        .limit(1)
    )
    if referral_id is None and party_id is not None:
        referral_id = db.scalar(
            select(Referral.id)
            .where(Referral.referred_party_id == party_id)
            .where(Referral.is_active.is_(True))
            .order_by(Referral.created_at.asc())
            .limit(1)
        )
    return referral_id


def qualify_referral_for_subscriber(
    db: Session, command: QualifyReferralForSubscriberCommand
) -> ReferralTransitionResult:
    _validate_context(command.context)

    def operation() -> ReferralTransitionResult:
        observed = db.get(Subscriber, command.subscriber_id)
        if observed is None:
            return _transition_result(
                None, outcome="not_applicable", context=command.context
            )
        referral_id = _find_referral_for_subscriber(db, observed.id, observed.party_id)
        if referral_id is None:
            return _transition_result(
                None, outcome="not_applicable", context=command.context
            )
        referral = _lock_referral(db, referral_id)
        subscriber = _lock_subscriber(db, command.subscriber_id)
        if not _subscriber_is_active(db, subscriber):
            return _transition_result(
                referral, outcome="not_applicable", context=command.context
            )
        policy = _program_policy(db)
        if not policy.enabled:
            return _transition_result(
                referral, outcome="not_applicable", context=command.context
            )
        if (
            referral.referred_party_id is not None
            and referral.status == ReferralStatus.pending.value
        ):
            Referrals.attach_subscriber_for_conversion(
                db,
                referral_id=str(referral.id),
                subscriber_id=str(subscriber.id),
                source=command.context.actor,
                reason=command.context.reason,
            )
        if referral.status in {
            ReferralStatus.qualified.value,
            ReferralStatus.rewarded.value,
        }:
            return _transition_result(
                referral, outcome="already_qualified", context=command.context
            )
        if referral.status != ReferralStatus.pending.value:
            return _transition_result(
                referral, outcome="not_applicable", context=command.context
            )

        now = datetime.now(UTC)
        created = referral.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created is not None and now - created > timedelta(days=policy.window_days):
            referral.status = ReferralStatus.expired.value
            db.flush()
            _stage_referral_evidence(
                db,
                referral=referral,
                event_type=EventType.referral_expired,
                action="referrals.expired",
                outcome="expired",
                context=command.context,
            )
            return _transition_result(
                referral, outcome="expired", context=command.context
            )

        referral.status = ReferralStatus.qualified.value
        referral.qualified_at = now
        referral.reward_amount = policy.amount
        referral.reward_currency = policy.currency
        referral.reward_status = (
            ReferralRewardStatus.approved.value
            if policy.auto_approve
            else ReferralRewardStatus.pending.value
        )
        db.flush()
        _stage_referral_evidence(
            db,
            referral=referral,
            event_type=EventType.referral_qualified,
            action="referrals.qualified",
            outcome="qualified",
            context=command.context,
            extra={"amount": str(policy.amount), "currency": policy.currency},
        )
        return _transition_result(
            referral, outcome="qualified", context=command.context
        )

    return _execute(
        db,
        definition=_QUALIFY_COMMAND,
        context=command.context,
        operation=operation,
    )


def qualify_referral_override(
    db: Session, command: QualifyReferralOverrideCommand
) -> ReferralTransitionResult:
    _validate_context(command.context)

    def operation() -> ReferralTransitionResult:
        referral = _lock_referral(db, command.referral_id)
        if referral.status in {
            ReferralStatus.qualified.value,
            ReferralStatus.rewarded.value,
        }:
            return _transition_result(
                referral, outcome="already_qualified", context=command.context
            )
        if referral.status not in {
            ReferralStatus.pending.value,
            ReferralStatus.expired.value,
        }:
            raise _error(
                "invalid_transition",
                f"Referral in status '{referral.status}' cannot be qualified.",
            )
        if referral.referred_party_id is not None:
            if referral.referred_subscriber_id is None:
                raise _error(
                    "account_attachment_required",
                    "Attach the reviewed Subscriber account before qualification.",
                )
            Referrals.attach_subscriber_for_conversion(
                db,
                referral_id=str(referral.id),
                subscriber_id=str(referral.referred_subscriber_id),
                source=command.context.actor,
                reason=command.context.reason,
            )
        policy = _program_policy(db)
        referral.status = ReferralStatus.qualified.value
        referral.qualified_at = datetime.now(UTC)
        referral.reward_amount = policy.amount
        referral.reward_currency = policy.currency
        referral.reward_status = (
            ReferralRewardStatus.approved.value
            if policy.auto_approve
            else ReferralRewardStatus.pending.value
        )
        db.flush()
        _stage_referral_evidence(
            db,
            referral=referral,
            event_type=EventType.referral_qualified,
            action="referrals.qualification_overridden",
            outcome="qualified",
            context=command.context,
            extra={"amount": str(policy.amount), "currency": policy.currency},
        )
        return _transition_result(
            referral, outcome="qualified", context=command.context
        )

    return _execute(
        db,
        definition=_QUALIFY_OVERRIDE_COMMAND,
        context=command.context,
        operation=operation,
    )


def reject_referral(
    db: Session, command: RejectReferralCommand
) -> ReferralTransitionResult:
    _validate_context(command.context)
    reason = _required(command.reason, "reason", max_length=200)

    def operation() -> ReferralTransitionResult:
        referral = _lock_referral(db, command.referral_id)
        marker = f"Rejected: {reason}"
        if referral.status == ReferralStatus.rejected.value:
            if marker not in str(referral.notes or ""):
                raise _error(
                    "idempotency_conflict",
                    "Referral was already rejected with different evidence.",
                )
            return _transition_result(
                referral, outcome="already_rejected", context=command.context
            )
        if (
            referral.status == ReferralStatus.rewarded.value
            or referral.reward_status == ReferralRewardStatus.issued.value
        ):
            raise _error(
                "invalid_transition",
                "An issued referral reward cannot be rejected.",
            )
        referral.status = ReferralStatus.rejected.value
        referral.reward_status = ReferralRewardStatus.void.value
        referral.notes = f"{referral.notes}\n{marker}" if referral.notes else marker
        db.flush()
        _stage_referral_evidence(
            db,
            referral=referral,
            event_type=EventType.referral_rejected,
            action="referrals.rejected",
            outcome="rejected",
            context=command.context,
        )
        return _transition_result(referral, outcome="rejected", context=command.context)

    return _execute(
        db,
        definition=_REJECT_COMMAND,
        context=command.context,
        operation=operation,
    )


def issue_referral_reward(
    db: Session, command: IssueReferralRewardCommand
) -> ReferralTransitionResult:
    _validate_context(command.context)

    def operation() -> ReferralTransitionResult:
        referral = _lock_referral(db, command.referral_id)
        if referral.status not in {
            ReferralStatus.qualified.value,
            ReferralStatus.rewarded.value,
        }:
            raise _error(
                "invalid_transition",
                f"Referral in status '{referral.status}' cannot receive a reward.",
            )
        amount = referral.reward_amount
        if amount is None or amount <= 0:
            raise _error(
                "invalid_reward",
                "Referral has no positive reward amount to issue.",
            )
        currency = str(referral.reward_currency or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise _error(
                "incomplete_reward_evidence",
                "Referral reward currency evidence is incomplete.",
            )
        existing_credit_id = str(
            (referral.metadata_ or {}).get("reward_credit_id") or ""
        )
        if (
            referral.status == ReferralStatus.rewarded.value
            and referral.reward_status == ReferralRewardStatus.issued.value
            and existing_credit_id
        ):
            return _transition_result(
                referral,
                outcome="already_rewarded",
                context=command.context,
                credit_note_id=UUID(existing_credit_id),
            )

        credit_result = CreditNotes.issue_referral_reward(
            db,
            referral_id=referral.id,
            account_id=referral.referrer_subscriber_id,
            amount=amount,
            currency=currency,
        )
        meta = dict(referral.metadata_ or {})
        meta["reward_credit_id"] = str(credit_result.credit_note.id)
        meta["reward_subscriber_id"] = str(referral.referrer_subscriber_id)
        referral.metadata_ = meta
        referral.reward_status = ReferralRewardStatus.issued.value
        referral.reward_issued_at = referral.reward_issued_at or datetime.now(UTC)
        referral.status = ReferralStatus.rewarded.value
        db.flush()
        reconciled = credit_result.idempotent_replay
        _stage_referral_evidence(
            db,
            referral=referral,
            event_type=(
                EventType.referral_reward_reconciled
                if reconciled
                else EventType.referral_reward_issued
            ),
            action=(
                "referrals.reward_reconciled"
                if reconciled
                else "referrals.reward_issued"
            ),
            outcome="reward_reconciled" if reconciled else "reward_issued",
            context=command.context,
            extra={
                "amount": f"{currency} {amount}",
                "currency": currency,
                "credit_note_id": str(credit_result.credit_note.id),
            },
        )
        logger.info(
            "referral_reward_recorded",
            extra={
                "referral_id": str(referral.id),
                "referrer_subscriber_id": str(referral.referrer_subscriber_id),
                "credit_note_id": str(credit_result.credit_note.id),
                "outcome": "reconciled" if reconciled else "issued",
            },
        )
        return _transition_result(
            referral,
            outcome="reward_reconciled" if reconciled else "reward_issued",
            context=command.context,
            credit_note_id=credit_result.credit_note.id,
        )

    return _execute(
        db,
        definition=_ISSUE_REWARD_COMMAND,
        context=command.context,
        operation=operation,
    )


class Referrals:
    """Read owner plus the nested canonical attachment writer."""

    @staticmethod
    def program(db: Session) -> dict[str, object]:
        policy = _program_policy(db)
        return {
            "enabled": policy.enabled,
            "amount": policy.amount,
            "currency": policy.currency,
        }

    @staticmethod
    def get_by_code(db: Session, code: str) -> ReferralCode | None:
        normalized = str(code or "").strip().upper()
        if not normalized:
            return None
        return db.scalars(
            select(ReferralCode)
            .where(ReferralCode.code == normalized)
            .where(ReferralCode.is_active.is_(True))
        ).one_or_none()

    @staticmethod
    def get_code(db: Session, referral_code_id: UUID) -> ReferralCode:
        code = db.get(ReferralCode, referral_code_id)
        if code is None or not code.is_active:
            raise _error("code_not_found", "Referral code not found.")
        return code

    @staticmethod
    def get(db: Session, referral_id: str | UUID) -> Referral:
        try:
            resolved_id = UUID(str(referral_id))
        except ValueError as exc:
            raise _error("referral_not_found", "Referral not found.") from exc
        referral = db.get(Referral, resolved_id)
        if referral is None or not referral.is_active:
            raise _error("referral_not_found", "Referral not found.")
        return referral

    @staticmethod
    def list(
        db: Session,
        *,
        status: str | None = None,
        reward_status: str | None = None,
        referrer_subscriber_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Referral]:
        query = select(Referral).where(Referral.is_active.is_(True))
        if status:
            try:
                resolved_status = ReferralStatus(status).value
            except ValueError as exc:
                raise _error(
                    "invalid_filter", "Invalid referral status filter."
                ) from exc
            query = query.where(Referral.status == resolved_status)
        if reward_status:
            try:
                resolved_reward_status = ReferralRewardStatus(reward_status).value
            except ValueError as exc:
                raise _error(
                    "invalid_filter", "Invalid referral reward-status filter."
                ) from exc
            query = query.where(Referral.reward_status == resolved_reward_status)
        if referrer_subscriber_id:
            try:
                resolved_referrer_id = UUID(str(referrer_subscriber_id))
            except ValueError as exc:
                raise _error("invalid_filter", "Invalid referrer filter.") from exc
            query = query.where(Referral.referrer_subscriber_id == resolved_referrer_id)
        return list(
            db.scalars(
                query.order_by(Referral.created_at.desc()).limit(limit).offset(offset)
            ).all()
        )

    @staticmethod
    def attach_subscriber_for_conversion(
        db: Session,
        *,
        referral_id: str,
        subscriber_id: str,
        source: str,
        reason: str,
    ) -> Referral:
        """Write the exact reviewed account attachment without committing."""

        try:
            resolved_referral_id = UUID(str(referral_id))
            resolved_subscriber_id = UUID(str(subscriber_id))
        except ValueError as exc:
            raise ReferralAttachmentError(
                "context_not_found", "Referral or Subscriber identifier is invalid."
            ) from exc
        referral = db.get(Referral, resolved_referral_id)
        if referral is None or not referral.is_active:
            raise ReferralAttachmentError("context_not_found", "Referral not found")
        if referral.referred_party_id is None:
            raise ReferralAttachmentError(
                "incomplete_context",
                "Referral needs a reviewed Party binding before conversion.",
            )
        if not (
            referral.party_bound_at is not None
            and str(referral.party_binding_source or "").strip()
            and str(referral.party_binding_reason or "").strip()
        ):
            raise ReferralAttachmentError(
                "incomplete_context",
                "Referral has incomplete Party binding evidence.",
            )
        normalized_source = str(source or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_source or not normalized_reason:
            raise ReferralAttachmentError(
                "invalid_command", "Subscriber attachment requires source and reason."
            )
        subscriber = db.get(Subscriber, resolved_subscriber_id)
        if subscriber is None:
            raise ReferralAttachmentError(
                "subscriber_not_found", "Subscriber not found"
            )
        if subscriber.id == referral.referrer_subscriber_id:
            raise ReferralAttachmentError(
                "self_referral", "A referrer cannot self-refer."
            )
        if subscriber.party_id is None:
            raise ReferralAttachmentError(
                "account_conflict",
                "Subscriber needs a reviewed Party binding before conversion.",
            )
        if subscriber.party_id != referral.referred_party_id:
            raise ReferralAttachmentError(
                "account_conflict",
                "Subscriber Party does not match the referred Party.",
            )
        referrer = db.get(Subscriber, referral.referrer_subscriber_id)
        if referrer is not None and referrer.party_id == subscriber.party_id:
            raise ReferralAttachmentError(
                "self_referral", "A referrer cannot self-refer."
            )
        if (
            referral.referred_subscriber_id is not None
            and referral.referred_subscriber_id != subscriber.id
        ):
            raise ReferralAttachmentError(
                "account_conflict",
                "Referral is already attached to a different Subscriber.",
            )
        if referral.referred_lead_id is None:
            raise ReferralAttachmentError(
                "incomplete_context",
                "Referral needs its attributed Lead before conversion.",
            )
        complete_link_evidence = bool(
            referral.subscriber_linked_at is not None
            and str(referral.subscriber_link_source or "").strip()
            and str(referral.subscriber_link_reason or "").strip()
        )
        if (
            referral.referred_subscriber_id == subscriber.id
            and not complete_link_evidence
            and any(
                value is not None
                for value in (
                    referral.subscriber_linked_at,
                    referral.subscriber_link_source,
                    referral.subscriber_link_reason,
                )
            )
        ):
            raise ReferralAttachmentError(
                "incomplete_context",
                "Referral has incomplete Subscriber-link evidence.",
            )
        try:
            lead_lifecycle.attach_lead_subscriber(
                db,
                lead_id=referral.referred_lead_id,
                subscriber_id=subscriber.id,
                source=normalized_source,
                reason=normalized_reason,
            )
        except lead_lifecycle.LeadLifecycleError as exc:
            raise ReferralAttachmentError("account_conflict", str(exc)) from exc
        if referral.referred_subscriber_id == subscriber.id and complete_link_evidence:
            return referral
        referral.referred_subscriber_id = subscriber.id
        referral.subscriber_linked_at = datetime.now(UTC)
        referral.subscriber_link_source = normalized_source
        referral.subscriber_link_reason = normalized_reason
        db.flush()
        evidence = {
            "schema_version": 1,
            "referral_id": str(referral.id),
            "referred_party_id": str(referral.referred_party_id),
            "referred_lead_id": str(referral.referred_lead_id),
            "subscriber_id": str(subscriber.id),
        }
        stage_audit_event(
            db,
            action="referrals.subscriber_attached",
            entity_type="referral",
            entity_id=str(referral.id),
            actor_type=AuditActorType.system,
            actor_id=normalized_source,
            metadata={"owner": "referrals.program", **evidence},
        )
        emit_event(
            db,
            EventType.referral_subscriber_attached,
            evidence,
            actor=normalized_source,
            subscriber_id=subscriber.id,
        )
        return referral

    @staticmethod
    def read_for_subscriber(db: Session, subscriber_id: str) -> dict[str, object]:
        try:
            resolved_subscriber_id = UUID(str(subscriber_id))
        except ValueError as exc:
            raise _error("subscriber_not_found", "Subscriber not found.") from exc
        code = db.scalars(
            select(ReferralCode)
            .where(ReferralCode.subscriber_id == resolved_subscriber_id)
            .where(ReferralCode.is_active.is_(True))
            .order_by(ReferralCode.created_at.desc())
            .limit(1)
        ).one_or_none()
        if code is None:
            raise _error(
                "code_not_found",
                "Subscriber does not have an active referral code.",
            )
        policy = _program_policy(db)
        rows = db.scalars(
            select(Referral)
            .where(Referral.referrer_subscriber_id == resolved_subscriber_id)
            .where(Referral.is_active.is_(True))
            .order_by(Referral.created_at.desc())
        ).all()
        counts = {"total": 0, "pending": 0, "qualified": 0, "rewarded": 0}
        earned = Decimal("0")
        items: list[dict[str, object]] = []
        for referral in rows:
            counts["total"] += 1
            if referral.status in counts:
                counts[referral.status] += 1
            if referral.status == ReferralStatus.rewarded.value:
                earned += referral.reward_amount or Decimal("0")
            items.append(
                {
                    "id": str(referral.id),
                    "status": referral.status,
                    "referred_name": _referred_display_name(referral),
                    "reward_amount": (
                        str(referral.reward_amount)
                        if referral.reward_amount is not None
                        else None
                    ),
                    "reward_currency": referral.reward_currency,
                    "reward_status": referral.reward_status,
                    "created_at": (
                        referral.created_at.isoformat() if referral.created_at else None
                    ),
                    "qualified_at": (
                        referral.qualified_at.isoformat()
                        if referral.qualified_at
                        else None
                    ),
                }
            )
        return {
            "code": code.code,
            "share_url": f"{_share_base_url(db)}/r/{code.code}",
            "program": {
                "enabled": policy.enabled,
                "reward_amount": str(policy.amount),
                "reward_currency": policy.currency,
            },
            "totals": {**counts, "total_earned": str(earned)},
            "referrals": items,
        }


referrals = Referrals()
