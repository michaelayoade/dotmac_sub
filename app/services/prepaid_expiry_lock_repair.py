"""Reviewed recovery of prepaid locks erased by terminal expiry.

This coordinator never infers a suspension reason from account status. It
requires the exact historical prepaid lock that expiry resolved, current due
uncovered prepaid service, and a fresh fingerprint before asking the lifecycle
owner to recreate that one reason-scoped lock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import suspend_subscription
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_coverage import (
    PrepaidCoverageStatus,
    resolve_prepaid_service_coverage,
)
from app.services.walled_garden_policy import resolve_walled_garden_decision

_OWNER = "access.prepaid_expiry_lock_repair"
_CONCERN = "reviewed expiry-cleared prepaid lock recovery"
_DEFINITION = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="apply_prepaid_expiry_lock_repair",
)


class PrepaidExpiryLockRepairBlocker(StrEnum):
    subscription_missing = "subscription_missing"
    account_ineligible = "account_ineligible"
    not_suspended_prepaid = "not_suspended_prepaid"
    fixed_term_ended = "fixed_term_ended"
    billing_not_due = "billing_not_due"
    historical_lock_not_expiry_prepaid = "historical_lock_not_expiry_prepaid"
    active_prepaid_lock = "active_prepaid_lock"
    coverage_present = "coverage_present"
    coverage_unresolved = "coverage_unresolved"


class PrepaidExpiryLockRepairError(DomainError):
    """Fail-closed reviewed-repair error."""


@dataclass(frozen=True, slots=True)
class PrepaidExpiryLockRepairQuery:
    subscription_id: UUID
    historical_lock_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class PrepaidExpiryLockDiscoveryQuery:
    resolved_since: datetime
    resolved_before: datetime
    as_of: datetime
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class PrepaidExpiryLockRepairPreview:
    query: PrepaidExpiryLockRepairQuery
    fingerprint: str
    blockers: tuple[PrepaidExpiryLockRepairBlocker, ...]
    active_lock_ids: tuple[UUID, ...]
    coverage_status: PrepaidCoverageStatus | None
    access_mode: AccessRestrictionMode | None

    @property
    def repairable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class PrepaidExpiryLockRepairCommand:
    query: PrepaidExpiryLockRepairQuery
    expected_fingerprint: str
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PrepaidExpiryLockRepairOutcome:
    subscription_id: UUID
    historical_lock_id: UUID
    active_lock_id: UUID
    created: bool


def _error(code: str, message: str) -> PrepaidExpiryLockRepairError:
    return PrepaidExpiryLockRepairError(
        code=f"{_OWNER}.{code}", message=message, retryable=False
    )


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _source(
    historical_lock_id: UUID, idempotency_key: str, expected_fingerprint: str
) -> str:
    key_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    return (
        f"prepaid-expiry-lock-repair:{historical_lock_id}:"
        f"{key_digest}:{expected_fingerprint}"
    )


def _validate_as_of(value: datetime) -> None:
    if value.tzinfo is None:
        raise _error("naive_as_of", "Repair reference time must have a timezone.")
    if value > datetime.now(UTC) + timedelta(seconds=30):
        raise _error("future_as_of", "Repair reference time cannot be in the future.")
    if datetime.now(UTC) - value > timedelta(minutes=15):
        raise _error("stale_as_of", "Repair preview is too old; use current time.")


def discover_prepaid_expiry_lock_repairs(
    db: Session, query: PrepaidExpiryLockDiscoveryQuery
) -> tuple[PrepaidExpiryLockRepairPreview, ...]:
    """List exact historical-lock candidates for operator review, without writes."""
    _validate_as_of(query.as_of)
    if (
        query.resolved_since.tzinfo is None
        or query.resolved_before.tzinfo is None
        or query.resolved_since >= query.resolved_before
        or not 1 <= query.limit <= 1000
        or not 0 <= query.offset <= 100000
    ):
        raise _error(
            "invalid_discovery", "Use a valid bounded expiry window and limit."
        )
    rows = db.execute(
        select(Subscription.id, EnforcementLock.id)
        .join(
            EnforcementLock,
            EnforcementLock.subscription_id == Subscription.id,
        )
        .where(
            Subscription.status == SubscriptionStatus.suspended,
            Subscription.billing_mode == BillingMode.prepaid,
            Subscription.next_billing_at.is_not(None),
            Subscription.next_billing_at <= query.as_of,
            EnforcementLock.reason == EnforcementReason.prepaid,
            EnforcementLock.is_active.is_(False),
            EnforcementLock.resolved_by == "expired",
            EnforcementLock.resolved_at >= query.resolved_since,
            EnforcementLock.resolved_at < query.resolved_before,
        )
        .order_by(
            Subscription.id,
            EnforcementLock.resolved_at.desc(),
            EnforcementLock.id,
        )
        .limit(query.limit)
        .offset(query.offset)
    ).all()
    return tuple(
        preview_prepaid_expiry_lock_repair(
            db,
            PrepaidExpiryLockRepairQuery(
                subscription_id=subscription_id,
                historical_lock_id=lock_id,
                as_of=query.as_of,
            ),
        )
        for subscription_id, lock_id in rows
    )


def preview_prepaid_expiry_lock_repair(
    db: Session, query: PrepaidExpiryLockRepairQuery
) -> PrepaidExpiryLockRepairPreview:
    """Read-only exact-service preview; no lock or financial fact is written."""
    _validate_as_of(query.as_of)

    subscription = db.get(Subscription, query.subscription_id)
    historical_lock = db.get(EnforcementLock, query.historical_lock_id)
    account = (
        db.get(Subscriber, subscription.subscriber_id)
        if subscription is not None
        else None
    )
    active_locks = (
        tuple(
            db.scalars(
                select(EnforcementLock)
                .where(
                    EnforcementLock.subscription_id == subscription.id,
                    EnforcementLock.is_active.is_(True),
                )
                .order_by(EnforcementLock.id)
            ).all()
        )
        if subscription is not None
        else ()
    )
    blockers: list[PrepaidExpiryLockRepairBlocker] = []
    if subscription is None:
        blockers.append(PrepaidExpiryLockRepairBlocker.subscription_missing)
    else:
        if (
            account is None
            or not account.is_active
            or not account.billing_enabled
            or account.status == SubscriberStatus.canceled
        ):
            blockers.append(PrepaidExpiryLockRepairBlocker.account_ineligible)
        if (
            subscription.billing_mode != BillingMode.prepaid
            or subscription.status != SubscriptionStatus.suspended
        ):
            blockers.append(PrepaidExpiryLockRepairBlocker.not_suspended_prepaid)
        if subscription.end_at is not None and _utc(subscription.end_at) <= _utc(
            query.as_of
        ):
            blockers.append(PrepaidExpiryLockRepairBlocker.fixed_term_ended)
        if subscription.next_billing_at is None or _utc(
            subscription.next_billing_at
        ) > _utc(query.as_of):
            blockers.append(PrepaidExpiryLockRepairBlocker.billing_not_due)
        if any(lock.reason is EnforcementReason.prepaid for lock in active_locks):
            blockers.append(PrepaidExpiryLockRepairBlocker.active_prepaid_lock)

    if (
        historical_lock is None
        or historical_lock.subscription_id != query.subscription_id
        or (
            subscription is not None
            and historical_lock.subscriber_id != subscription.subscriber_id
        )
        or historical_lock.reason is not EnforcementReason.prepaid
        or historical_lock.is_active
        or historical_lock.resolved_by != "expired"
        or historical_lock.resolved_at is None
        or (
            historical_lock.resolved_at is not None
            and _utc(historical_lock.resolved_at) > _utc(query.as_of)
        )
    ):
        blockers.append(
            PrepaidExpiryLockRepairBlocker.historical_lock_not_expiry_prepaid
        )

    coverage_status: PrepaidCoverageStatus | None = None
    if subscription is not None and subscription.billing_mode == BillingMode.prepaid:
        decision = resolve_prepaid_service_coverage(
            db, [subscription], as_of=query.as_of
        )[subscription.id]
        coverage_status = decision.status
        if coverage_status is PrepaidCoverageStatus.covered:
            blockers.append(PrepaidExpiryLockRepairBlocker.coverage_present)
        elif coverage_status is PrepaidCoverageStatus.unresolved_projection:
            blockers.append(PrepaidExpiryLockRepairBlocker.coverage_unresolved)

    access_decision = (
        resolve_walled_garden_decision(
            db,
            account,
            requested_mode=AccessRestrictionMode.captive,
            subscription=subscription,
        )
        if account is not None and subscription is not None
        else None
    )

    payload = {
        "subscription_id": str(query.subscription_id),
        "historical_lock_id": str(query.historical_lock_id),
        "as_of": _stamp(query.as_of),
        "subscription_status": subscription.status.value if subscription else None,
        "billing_mode": subscription.billing_mode.value if subscription else None,
        "end_at": _stamp(subscription.end_at) if subscription else None,
        "next_billing_at": _stamp(subscription.next_billing_at)
        if subscription
        else None,
        "account_status": account.status.value if account else None,
        "account_active": account.is_active if account else None,
        "billing_enabled": account.billing_enabled if account else None,
        "historical_lock": (
            {
                "subscription_id": str(historical_lock.subscription_id),
                "reason": historical_lock.reason.value,
                "active": historical_lock.is_active,
                "resolved_by": historical_lock.resolved_by,
                "resolved_at": _stamp(historical_lock.resolved_at),
            }
            if historical_lock
            else None
        ),
        "active_locks": [
            (str(lock.id), lock.reason.value, lock.source) for lock in active_locks
        ],
        "coverage_status": coverage_status.value if coverage_status else None,
        "access_decision": access_decision.as_dict() if access_decision else None,
        "blockers": [blocker.value for blocker in blockers],
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PrepaidExpiryLockRepairPreview(
        query=query,
        fingerprint=fingerprint,
        blockers=tuple(blockers),
        active_lock_ids=tuple(lock.id for lock in active_locks),
        coverage_status=coverage_status,
        access_mode=(access_decision.effective_mode if access_decision else None),
    )


def apply_prepaid_expiry_lock_repair(
    db: Session, command: PrepaidExpiryLockRepairCommand
) -> PrepaidExpiryLockRepairOutcome:
    """Recheck reviewed evidence, then recreate one prepaid lock atomically."""
    if not command.context.idempotency_key:
        raise _error("idempotency_required", "A stable idempotency key is required.")
    if len(command.expected_fingerprint) != 64 or any(
        character not in "0123456789abcdef"
        for character in command.expected_fingerprint
    ):
        raise _error(
            "invalid_preview_fingerprint", "A SHA-256 preview fingerprint is required."
        )

    def operation() -> PrepaidExpiryLockRepairOutcome:
        # Payment/enforcement coordinators take the account lock before child
        # lifecycle locks. Re-read the subscription after both locks are held.
        subscription = db.get(Subscription, command.query.subscription_id)
        if subscription is not None:
            db.execute(
                select(Subscriber.id)
                .where(Subscriber.id == subscription.subscriber_id)
                .with_for_update()
            ).first()
        db.execute(
            select(Subscription.id)
            .where(Subscription.id == command.query.subscription_id)
            .with_for_update()
        ).first()
        db.execute(
            select(EnforcementLock.id)
            .where(EnforcementLock.id == command.query.historical_lock_id)
            .with_for_update()
        ).first()
        # The caller may have previewed these rows in this Session. A row lock
        # does not replace already-loaded ORM attributes, so force the
        # fingerprint recheck to read post-lock database state.
        db.expire_all()

        source = _source(
            command.query.historical_lock_id,
            command.context.idempotency_key or "",
            command.expected_fingerprint,
        )
        existing = db.scalar(
            select(EnforcementLock).where(
                EnforcementLock.subscription_id == command.query.subscription_id,
                EnforcementLock.reason == EnforcementReason.prepaid,
                EnforcementLock.is_active.is_(True),
                EnforcementLock.source == source,
            )
        )
        if existing is not None:
            if (
                subscription is None
                or subscription.status != SubscriptionStatus.suspended
            ):
                raise _error(
                    "repair_blocked", "Repaired service is no longer suspended."
                )
            return PrepaidExpiryLockRepairOutcome(
                subscription_id=command.query.subscription_id,
                historical_lock_id=command.query.historical_lock_id,
                active_lock_id=existing.id,
                created=False,
            )

        preview = preview_prepaid_expiry_lock_repair(db, command.query)
        if preview.fingerprint != command.expected_fingerprint:
            raise _error("stale_preview", "Repair evidence changed; preview again.")
        if preview.blockers:
            raise _error("repair_blocked", "Repair preview is not eligible.")

        lock = suspend_subscription(
            db,
            str(command.query.subscription_id),
            reason=EnforcementReason.prepaid,
            source=source,
            access_mode=preview.access_mode or AccessRestrictionMode.hard_reject,
            requested_access_mode=AccessRestrictionMode.captive,
            notes=command.context.reason,
            evidence_context=command.context,
        )
        return PrepaidExpiryLockRepairOutcome(
            subscription_id=command.query.subscription_id,
            historical_lock_id=command.query.historical_lock_id,
            active_lock_id=lock.id,
            created=True,
        )

    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=operation,
    )
