"""``customer.account_recovery`` — fail-closed, participant-gated deletion recovery.

Owns exactly four concerns: (1) deletion/recovery eligibility, (2)
tombstones, (3) re-baselining, (4) recovery confirmation. It does NOT decide
or mutate subscription state directly — only the registered ``subscription``
participant (``access.subscription_lifecycle`` / ``account_lifecycle.py``)
may reverse a canceled subscription, via
``restore_subscription_detailed(..., intent=ActivationIntent.DELETION_RECOVERY)``.

Every public entry point takes and returns immutable typed dataclasses —
never a dict, never an ``HTTPException``. Transport-layer error translation
belongs to the adapter (``app/web/admin/system.py``), not here.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account_recovery import (
    KNOWN_RESOURCE_TYPES,
    RESOURCE_TYPE_ADD_ON,
    RESOURCE_TYPE_ENFORCEMENT_LOCK,
    RESOURCE_TYPE_IP_ASSIGNMENT,
    AccountRecoveryRecord,
    AccountRecoveryState,
    AccountRecoverySubscriptionSnapshot,
)
from app.models.catalog import Subscription, SubscriptionAddOn
from app.models.enforcement_lock import EnforcementLock
from app.models.network import IPAssignment
from app.models.subscriber import Subscriber
from app.services.account_lifecycle import ActivationIntent, restore_subscription_detailed
from app.services.audit_adapter import stage_audit_event
from app.models.audit import AuditActorType
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType

# The closed set of resource types this owner can actually reverse today.
# Every other known type (see `KNOWN_RESOURCE_TYPES`) exists only so a
# legacy row can name what it touched without a false "subscription-only"
# claim; recovery on a record naming any of them fails closed.
REGISTERED_RECOVERY_PARTICIPANTS: frozenset[str] = frozenset({"subscription"})

# `cancel_subscription` (access.subscription_lifecycle) has other
# consequences beyond a bare status write — it ends active add-ons,
# resolves active enforcement locks, and releases the subscriber's active
# service IP assignments. None of those is a registered recovery
# participant, so a deletion that would trigger one of them is refused
# BEFORE any mutation (see `_preflight_unsupported_consequences`) rather
# than discovered later at restore time as `blocked_missing_participants`.
_UNSUPPORTED_CANCELLATION_CONSEQUENCES: frozenset[str] = frozenset(
    {RESOURCE_TYPE_ADD_ON, RESOURCE_TYPE_ENFORCEMENT_LOCK, RESOURCE_TYPE_IP_ASSIGNMENT}
)


class RecoveryOutcomeKind(StrEnum):
    restored = "restored"
    partially_restored = "partially_restored"
    blocked_missing_participants = "blocked_missing_participants"
    blocked_unsupported_consequence = "blocked_unsupported_consequence"


@dataclass(frozen=True, slots=True)
class RequestRecoverableDeletionCommand:
    """Create one new deletion generation for an account.

    Only used by the reviewed administrative recovery-eligible deletion
    path — self-service deletion (`account_deletion.py`) uses
    `customer_requested_termination` through the ordinary lifecycle owner
    and never creates a row here, because that path never claims to be
    recoverable.
    """

    account_id: UUID
    command_id: UUID
    correlation_id: UUID
    requested_by: str
    deleted_by: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class DeletionTombstone:
    """The typed, immutable result of opening one deletion generation."""

    record_id: UUID
    account_id: UUID
    generation: int
    confirmation_fingerprint: str
    affected_resource_types: tuple[str, ...]
    affected_subscription_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class DeletionPreflightBlocked:
    """Refusal to open a deletion generation: zero mutation happened.

    Returned when any subscription `request_recoverable_deletion` would
    cancel currently carries a consequence `cancel_subscription` would
    trigger that this owner cannot yet reverse (an active add-on, an active
    enforcement lock, or an active service IP assignment). Naming exactly
    which consequence(s) blocked the request lets an operator resolve them
    (end the add-on, clear the lock, release the IP) and retry, instead of
    discovering the gap only at restore time.
    """

    kind: RecoveryOutcomeKind = field(
        default=RecoveryOutcomeKind.blocked_unsupported_consequence, init=False
    )
    account_id: UUID
    blocked_subscription_ids: tuple[UUID, ...]
    unsupported_consequences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreAccountCommand:
    account_id: UUID
    confirmation_fingerprint: str
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryBlocked:
    kind: RecoveryOutcomeKind = field(
        default=RecoveryOutcomeKind.blocked_missing_participants, init=False
    )
    record_id: UUID
    missing_participant_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoveryRestored:
    kind: RecoveryOutcomeKind = field(
        default=RecoveryOutcomeKind.restored, init=False
    )
    record_id: UUID
    account_id: UUID
    restored_subscription_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class RecoveryPartiallyRestored:
    """Some, or none, of the generation's subscriptions actually reactivated.

    The record is left in its existing ``open``/``blocked`` state (not
    ``restored``) precisely so a subsequent `restore_account` call can retry
    — closing the generation on a partial outcome would strand the
    unrestored subscriptions with no way back in.
    """

    kind: RecoveryOutcomeKind = field(
        default=RecoveryOutcomeKind.partially_restored, init=False
    )
    record_id: UUID
    account_id: UUID
    restored_subscription_ids: tuple[UUID, ...]
    unrestored_subscription_ids: tuple[UUID, ...]


RecoveryOutcome = RecoveryBlocked | RecoveryRestored | RecoveryPartiallyRestored


@dataclass(frozen=True, slots=True)
class RebaselineRecoveryCommand:
    account_id: UUID
    confirmation_fingerprint: str
    affected_resource_types: tuple[str, ...]
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class RebaselineApplied:
    record_id: UUID
    new_confirmation_fingerprint: str
    fingerprint_revision: int
    affected_resource_types: tuple[str, ...]


class AccountRecoveryError(DomainError):
    """Stable, typed failure. Never an HTTPException — see module docstring."""


def _error(suffix: str, message: str, **details: object) -> AccountRecoveryError:
    return AccountRecoveryError(
        code=f"customer.account_recovery.{suffix}",
        message=message,
        details=details,
    )


def _fingerprint(
    *,
    account_id: UUID,
    generation: int,
    deletion_intent: str,
    affected_resource_types: tuple[str, ...],
    revision: int,
) -> str:
    canonical = "|".join(
        (
            str(account_id),
            str(generation),
            deletion_intent,
            ",".join(sorted(affected_resource_types)),
            str(revision),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lock_subscriber(db: Session, account_id: UUID) -> Subscriber:
    subscriber = db.execute(
        select(Subscriber).where(Subscriber.id == account_id).with_for_update()
    ).scalar_one_or_none()
    if subscriber is None:
        raise _error(
            "account_not_found",
            f"Account {account_id} not found",
            account_id=str(account_id),
        )
    return subscriber


def _lock_open_record(db: Session, account_id: UUID) -> AccountRecoveryRecord:
    record = db.execute(
        select(AccountRecoveryRecord)
        .where(
            AccountRecoveryRecord.account_id == account_id,
            AccountRecoveryRecord.state.in_(
                (AccountRecoveryState.open, AccountRecoveryState.blocked)
            ),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if record is None:
        raise _error(
            "no_open_generation",
            f"Account {account_id} has no open recovery generation",
            account_id=str(account_id),
        )
    return record


def _preflight_unsupported_consequences(
    db: Session, subscriptions: list[Subscription]
) -> tuple[str, ...]:
    """Return every unsupported `cancel_subscription` consequence in play.

    Only subscriptions that are not already `canceled` matter — exactly the
    set `request_recoverable_deletion` is about to cancel. Read-only: no
    locking beyond the row locks the caller already holds, no mutation.
    """
    pending_ids = [s.id for s in subscriptions if s.status.value != "canceled"]
    if not pending_ids:
        return ()

    found: set[str] = set()

    if db.execute(
        select(EnforcementLock.id)
        .where(
            EnforcementLock.subscription_id.in_(pending_ids),
            EnforcementLock.is_active.is_(True),
        )
        .limit(1)
    ).first():
        found.add(RESOURCE_TYPE_ENFORCEMENT_LOCK)

    if db.execute(
        select(SubscriptionAddOn.id)
        .where(
            SubscriptionAddOn.subscription_id.in_(pending_ids),
            SubscriptionAddOn.end_at.is_(None),
        )
        .limit(1)
    ).first():
        found.add(RESOURCE_TYPE_ADD_ON)

    # IPAssignment is keyed by subscriber (account), not subscription — a
    # release triggered by cancelling any one pending subscription affects
    # the whole account's active service IPs (see
    # `ip_lifecycle.release_service_ips_for_subscription`), so this check is
    # account-wide rather than per-subscription.
    account_id = subscriptions[0].subscriber_id
    if db.execute(
        select(IPAssignment.id)
        .where(
            IPAssignment.subscriber_id == account_id,
            IPAssignment.is_active.is_(True),
        )
        .limit(1)
    ).first():
        found.add(RESOURCE_TYPE_IP_ASSIGNMENT)

    return tuple(sorted(found))


def request_recoverable_deletion(
    db: Session, command: RequestRecoverableDeletionCommand
) -> DeletionTombstone | DeletionPreflightBlocked:
    """Open one new deletion generation and tombstone the account.

    Locks the subscriber first, then every affected subscription in stable
    UUID order, exactly matching the lock order `restore_account` uses so
    the two operations can never deadlock against each other.

    Before any mutation, every subscription that would be canceled is
    checked for an unsupported `cancel_subscription` consequence (an active
    add-on, an active enforcement lock, or an active service IP
    assignment). Subscription is the only registered recovery participant
    in this slice — any of those additional consequences refuses the whole
    request with zero mutation (`DeletionPreflightBlocked`) rather than
    proceeding and leaving a tombstone that can never fully restore.
    """
    from app.models.account_recovery import ADMINISTRATIVE_RECOVERABLE_DELETION
    from app.services.account_lifecycle import cancel_subscription
    from app.services.billing_automation import CancellationCreditIntent

    subscriber = _lock_subscriber(db, command.account_id)

    existing_open = db.execute(
        select(AccountRecoveryRecord.id).where(
            AccountRecoveryRecord.account_id == command.account_id,
            AccountRecoveryRecord.state.in_(
                (AccountRecoveryState.open, AccountRecoveryState.blocked)
            ),
        )
    ).first()
    if existing_open is not None:
        raise _error(
            "generation_already_open",
            f"Account {command.account_id} already has an open recovery generation",
            account_id=str(command.account_id),
        )

    prior_generation = db.execute(
        select(AccountRecoveryRecord.generation)
        .where(AccountRecoveryRecord.account_id == command.account_id)
        .order_by(AccountRecoveryRecord.generation.desc())
        .limit(1)
    ).scalar()
    generation = (prior_generation or 0) + 1

    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == command.account_id)
            .order_by(Subscription.id)
            .with_for_update()
        ).all()
    )

    if subscriptions:
        unsupported = _preflight_unsupported_consequences(db, subscriptions)
        if unsupported:
            blocked_ids = tuple(
                s.id for s in subscriptions if s.status.value != "canceled"
            )
            return DeletionPreflightBlocked(
                account_id=command.account_id,
                blocked_subscription_ids=blocked_ids,
                unsupported_consequences=unsupported,
            )

    now = datetime.now(UTC)

    # This owner tombstones the account; driving each subscription to
    # `canceled` is still exclusively the lifecycle owner's decision
    # (`access.subscription_lifecycle` / `cancel_subscription`), invoked here
    # with the one credit intent that suppresses a cancellation credit
    # (`administrative_recoverable_deletion` — the subscription is expected
    # to be reversed, not settled as a real termination).
    pre_deletion_statuses = {s.id: s.status.value for s in subscriptions}
    pre_deletion_offer_versions = {s.id: s.offer_version_id for s in subscriptions}
    for subscription in subscriptions:
        if subscription.status.value != "canceled":
            cancel_subscription(
                db,
                str(subscription.id),
                command.reason or "Recoverable administrative account deletion",
                command.deleted_by,
                credit_intent=CancellationCreditIntent.ADMINISTRATIVE_RECOVERABLE_DELETION,
                emit=True,
            )
    subscriber.is_active = False

    affected_types = ("subscription",) if subscriptions else ()
    fingerprint = _fingerprint(
        account_id=command.account_id,
        generation=generation,
        deletion_intent=ADMINISTRATIVE_RECOVERABLE_DELETION,
        affected_resource_types=affected_types,
        revision=1,
    )

    record = AccountRecoveryRecord(
        account_id=command.account_id,
        generation=generation,
        deletion_intent=ADMINISTRATIVE_RECOVERABLE_DELETION,
        requested_by=command.requested_by,
        deleted_by=command.deleted_by,
        reason=command.reason,
        requested_at=now,
        deleted_at=now,
        state=AccountRecoveryState.open,
        affected_resource_types=list(affected_types),
        command_id=command.command_id,
        correlation_id=command.correlation_id,
        confirmation_fingerprint=fingerprint,
        fingerprint_revision=1,
    )
    db.add(record)
    db.flush()

    for subscription in subscriptions:
        db.add(
            AccountRecoverySubscriptionSnapshot(
                recovery_record_id=record.id,
                subscription_id=subscription.id,
                # Captured BEFORE this command's own cancellation above, so a
                # subscription that was e.g. `active` at deletion time is
                # correctly restored to `active`, not `canceled`.
                pre_deletion_status=pre_deletion_statuses[subscription.id],
                pre_deletion_offer_version_id=pre_deletion_offer_versions[
                    subscription.id
                ],
            )
        )
    db.flush()

    stage_audit_event(
        db,
        action="customer.account_recovery.deletion_tombstoned",
        entity_type="subscriber",
        entity_id=str(command.account_id),
        actor_type=AuditActorType.user,
        actor_id=command.deleted_by,
        request_id=str(command.correlation_id),
        metadata={
            "record_id": str(record.id),
            "generation": generation,
            "affected_resource_types": list(affected_types),
        },
    )
    emit_event(
        db,
        EventType.account_recovery_deletion_tombstoned,
        {
            "account_id": str(command.account_id),
            "record_id": str(record.id),
            "generation": generation,
            "affected_resource_types": list(affected_types),
        },
        actor=command.deleted_by,
        account_id=subscriber.id,
    )

    return DeletionTombstone(
        record_id=record.id,
        account_id=command.account_id,
        generation=generation,
        confirmation_fingerprint=fingerprint,
        affected_resource_types=affected_types,
        affected_subscription_ids=tuple(s.id for s in subscriptions),
    )


def restore_account(db: Session, command: RestoreAccountCommand) -> RecoveryOutcome:
    """Reverse one open deletion generation, or say exactly why it cannot.

    Lock order: `Subscriber` row, then the current open recovery record,
    then every participant resource in stable UUID order (subscriptions,
    ordered by id) — the same order `request_recoverable_deletion` uses.
    """
    subscriber = _lock_subscriber(db, command.account_id)
    record = _lock_open_record(db, command.account_id)

    recomputed = _fingerprint(
        account_id=record.account_id,
        generation=record.generation,
        deletion_intent=record.deletion_intent,
        affected_resource_types=tuple(record.affected_resource_types),
        revision=record.fingerprint_revision,
    )
    if not hmac.compare_digest(recomputed, command.confirmation_fingerprint):
        raise _error(
            "fingerprint_mismatch",
            "Confirmation fingerprint does not match the current recovery "
            "evidence; re-review before restoring.",
            record_id=str(record.id),
        )

    affected = tuple(record.affected_resource_types)
    missing = sorted(set(affected) - REGISTERED_RECOVERY_PARTICIPANTS)
    unknown = sorted(set(affected) - KNOWN_RESOURCE_TYPES)
    if unknown:
        # Defensive: a type outside even the known vocabulary is treated the
        # same as missing — fail closed, never silently ignored.
        missing = sorted(set(missing) | set(unknown))

    if missing:
        record.state = AccountRecoveryState.blocked
        db.flush()
        return RecoveryBlocked(record_id=record.id, missing_participant_types=tuple(missing))

    snapshots = list(
        db.scalars(
            select(AccountRecoverySubscriptionSnapshot)
            .where(AccountRecoverySubscriptionSnapshot.recovery_record_id == record.id)
            .order_by(AccountRecoverySubscriptionSnapshot.subscription_id)
        ).all()
    )
    restored_ids: list[UUID] = []
    unrestored_ids: list[UUID] = []
    for snapshot in snapshots:
        result = restore_subscription_detailed(
            db,
            str(snapshot.subscription_id),
            trigger="account_recovery",
            resolved_by=command.actor,
            intent=ActivationIntent.DELETION_RECOVERY,
            notes=command.reason,
        )
        if result.subscription_reactivated:
            restored_ids.append(snapshot.subscription_id)
        else:
            unrestored_ids.append(snapshot.subscription_id)

    if unrestored_ids:
        # Fail closed: at least one subscription in this generation did not
        # reactivate (active-login conflict, remaining lock, etc). The
        # record stays in its current open/blocked state rather than
        # `restored` so the generation remains retryable — marking it
        # `restored` here would permanently close recovery while the
        # account can still be left inactive/canceled.
        db.flush()
        stage_audit_event(
            db,
            action="customer.account_recovery.partially_restored",
            entity_type="subscriber",
            entity_id=str(command.account_id),
            actor_type=AuditActorType.user,
            actor_id=command.actor,
            request_id=str(record.correlation_id),
            metadata={
                "record_id": str(record.id),
                "generation": record.generation,
                "restored_subscription_ids": [str(i) for i in restored_ids],
                "unrestored_subscription_ids": [str(i) for i in unrestored_ids],
            },
        )
        emit_event(
            db,
            EventType.account_recovery_partially_restored,
            {
                "account_id": str(command.account_id),
                "record_id": str(record.id),
                "restored_subscription_ids": [str(i) for i in restored_ids],
                "unrestored_subscription_ids": [str(i) for i in unrestored_ids],
            },
            actor=command.actor,
            account_id=subscriber.id,
        )
        return RecoveryPartiallyRestored(
            record_id=record.id,
            account_id=command.account_id,
            restored_subscription_ids=tuple(restored_ids),
            unrestored_subscription_ids=tuple(unrestored_ids),
        )

    record.state = AccountRecoveryState.restored
    record.restored_at = datetime.now(UTC)
    record.restored_by = command.actor
    db.flush()

    stage_audit_event(
        db,
        action="customer.account_recovery.restored",
        entity_type="subscriber",
        entity_id=str(command.account_id),
        actor_type=AuditActorType.user,
        actor_id=command.actor,
        request_id=str(record.correlation_id),
        metadata={
            "record_id": str(record.id),
            "generation": record.generation,
            "restored_subscription_ids": [str(i) for i in restored_ids],
        },
    )
    emit_event(
        db,
        EventType.account_recovery_restored,
        {
            "account_id": str(command.account_id),
            "record_id": str(record.id),
            "restored_subscription_ids": [str(i) for i in restored_ids],
        },
        actor=command.actor,
        account_id=subscriber.id,
    )

    return RecoveryRestored(
        record_id=record.id,
        account_id=command.account_id,
        restored_subscription_ids=tuple(restored_ids),
    )


def rebaseline_recovery_evidence(
    db: Session, command: RebaselineRecoveryCommand
) -> RebaselineApplied:
    """Reviewed correction of a generation's own evidence.

    Fingerprint-bound: the caller must present the CURRENT fingerprint to
    prove they reviewed the record before correcting it. The new
    ``affected_resource_types`` must be a superset of the existing set —
    re-baselining can add evidence of previously-missed cascade involvement,
    but must never remove a resource type just to make the participant gate
    pass; that would silently claim a narrower, false history.
    """
    record = _lock_open_record(db, command.account_id)

    recomputed = _fingerprint(
        account_id=record.account_id,
        generation=record.generation,
        deletion_intent=record.deletion_intent,
        affected_resource_types=tuple(record.affected_resource_types),
        revision=record.fingerprint_revision,
    )
    if not hmac.compare_digest(recomputed, command.confirmation_fingerprint):
        raise _error(
            "fingerprint_mismatch",
            "Confirmation fingerprint does not match the current recovery "
            "evidence; re-review before re-baselining.",
            record_id=str(record.id),
        )

    existing = set(record.affected_resource_types)
    new_types = set(command.affected_resource_types)
    if not existing.issubset(new_types):
        raise _error(
            "rebaseline_would_narrow_evidence",
            "Re-baselining must not remove a previously-recorded resource "
            f"type: {sorted(existing - new_types)}",
            record_id=str(record.id),
            removed=sorted(existing - new_types),
        )
    unknown = new_types - KNOWN_RESOURCE_TYPES
    if unknown:
        raise _error(
            "unknown_resource_type",
            f"Unknown resource type(s): {sorted(unknown)}",
            record_id=str(record.id),
            unknown=sorted(unknown),
        )

    record.affected_resource_types = sorted(new_types)
    record.fingerprint_revision += 1
    # Deliberately NOT a state transition: re-baselining corrects the
    # generation's own evidence but does not change its restorability, and
    # `state` must stay in the `open`/`blocked` set every other reader
    # (`_lock_open_record`, the eligibility query, the one-open-generation
    # partial index) recognizes as an active generation. The fact that this
    # record was re-baselined is captured entirely by
    # `rebaselined_at`/`rebaselined_by`/`rebaseline_reason` below.
    record.rebaselined_at = datetime.now(UTC)
    record.rebaselined_by = command.actor
    record.rebaseline_reason = command.reason
    new_fingerprint = _fingerprint(
        account_id=record.account_id,
        generation=record.generation,
        deletion_intent=record.deletion_intent,
        affected_resource_types=tuple(record.affected_resource_types),
        revision=record.fingerprint_revision,
    )
    record.confirmation_fingerprint = new_fingerprint
    db.flush()

    stage_audit_event(
        db,
        action="customer.account_recovery.rebaselined",
        entity_type="subscriber",
        entity_id=str(command.account_id),
        actor_type=AuditActorType.user,
        actor_id=command.actor,
        request_id=str(record.correlation_id),
        metadata={
            "record_id": str(record.id),
            "affected_resource_types": record.affected_resource_types,
            "fingerprint_revision": record.fingerprint_revision,
        },
    )
    emit_event(
        db,
        EventType.account_recovery_rebaselined,
        {
            "account_id": str(command.account_id),
            "record_id": str(record.id),
            "affected_resource_types": record.affected_resource_types,
            "fingerprint_revision": record.fingerprint_revision,
        },
        actor=command.actor,
        account_id=record.account_id,
    )

    return RebaselineApplied(
        record_id=record.id,
        new_confirmation_fingerprint=new_fingerprint,
        fingerprint_revision=record.fingerprint_revision,
        affected_resource_types=tuple(record.affected_resource_types),
    )


@dataclass(frozen=True, slots=True)
class RecoveryEligibility:
    """A read-only description of an account's current open recovery state."""

    account_id: UUID
    has_open_generation: bool
    record_id: UUID | None
    generation: int | None
    state: AccountRecoveryState | None
    affected_resource_types: tuple[str, ...]
    missing_participant_types: tuple[str, ...]
    confirmation_fingerprint: str | None


def describe_recovery_eligibility(db: Session, account_id: UUID) -> RecoveryEligibility:
    """Read-only query: never mutates, never locks for update."""
    record = db.execute(
        select(AccountRecoveryRecord)
        .where(
            AccountRecoveryRecord.account_id == account_id,
            AccountRecoveryRecord.state.in_(
                (AccountRecoveryState.open, AccountRecoveryState.blocked)
            ),
        )
        .order_by(AccountRecoveryRecord.generation.desc())
    ).scalars().first()
    if record is None:
        return RecoveryEligibility(
            account_id=account_id,
            has_open_generation=False,
            record_id=None,
            generation=None,
            state=None,
            affected_resource_types=(),
            missing_participant_types=(),
            confirmation_fingerprint=None,
        )
    affected = tuple(record.affected_resource_types)
    missing = tuple(sorted(set(affected) - REGISTERED_RECOVERY_PARTICIPANTS))
    return RecoveryEligibility(
        account_id=account_id,
        has_open_generation=True,
        record_id=record.id,
        generation=record.generation,
        state=record.state,
        affected_resource_types=affected,
        missing_participant_types=missing,
        confirmation_fingerprint=record.confirmation_fingerprint,
    )
