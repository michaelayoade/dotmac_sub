"""Subscription / account lifecycle state machine.

All subscription status mutations should go through the domain operations
in this module. Each operation validates preconditions, manages enforcement
locks, emits exactly one canonical event, and recomputes account status.

This module uses ``db.flush()`` intentionally so callers can compose
lifecycle operations within a single transaction. The caller is responsible
for committing or rolling back.

Usage::

    from app.services.account_lifecycle import (
        suspend_subscription,
        restore_subscription,
        activate_subscription,
        expire_subscription,
        cancel_subscription,
        compute_account_status,
    )

    lock = suspend_subscription(
        db, subscription_id,
        reason=EnforcementReason.overdue,
        source=f"dunning_case:{case_id}",
    )

    restored = restore_subscription(
        db, subscription_id,
        trigger="payment",
        resolved_by=f"payment:{payment_id}",
    )
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.events import emit_event
from app.services.events.types import EventType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed restorer triggers per enforcement reason
# ---------------------------------------------------------------------------

ALLOWED_RESTORERS: dict[EnforcementReason, set[str]] = {
    EnforcementReason.overdue: {"payment", "collections_resolution", "admin"},
    EnforcementReason.fup: {"cap_reset", "top_up", "admin"},
    EnforcementReason.prepaid: {"top_up", "payment", "admin"},
    EnforcementReason.admin: {"admin"},
    EnforcementReason.customer_hold: {"customer", "admin"},
    EnforcementReason.fraud: {"admin"},
    EnforcementReason.system: {"system", "admin"},
}

# Verify ALLOWED_RESTORERS covers every enum member at import time
_missing_restorers = set(EnforcementReason) - set(ALLOWED_RESTORERS.keys())
if _missing_restorers:
    raise RuntimeError(f"ALLOWED_RESTORERS missing reasons: {_missing_restorers}")

# Statuses that can be suspended
_SUSPENDABLE = {
    SubscriptionStatus.active,
    SubscriptionStatus.pending,
}

# Statuses treated as equivalent to "suspended" in account derivation
# (Splynx legacy statuses that mean the service is not running)
SUSPENDED_EQUIVALENT = {
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
}

# Terminal statuses — locks are resolved when entering these
_TERMINAL = {
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.disabled,
    SubscriptionStatus.hidden,
    SubscriptionStatus.archived,
}


# ---------------------------------------------------------------------------
# Domain operations
# ---------------------------------------------------------------------------


def suspend_subscription(
    db: Session,
    subscription_id: str,
    reason: EnforcementReason,
    source: str,
    *,
    notes: str | None = None,
    emit: bool = True,
) -> EnforcementLock:
    """Create an enforcement lock and suspend the subscription.

    If the subscription is already suspended, the lock is still created
    for audit purposes but no ``subscription_suspended`` event is emitted.
    The ``enforcement_lock_created`` event is always emitted.

    If an active lock with the same reason already exists for this
    subscription, the existing lock is returned (idempotent).

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        reason: Why the subscription is being suspended.
        source: Who/what initiated this (e.g. ``"dunning_case:{id}"``).
        notes: Optional human-readable notes.
        emit: Whether to emit events.

    Returns:
        The enforcement lock (new or existing duplicate).

    Raises:
        ValueError: If the subscription cannot be suspended.
    """
    # Lock the subscription row to prevent concurrent mutations
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    was_already_suspended = subscription.status in SUSPENDED_EQUIVALENT

    if subscription.status not in _SUSPENDABLE and not was_already_suspended:
        raise ValueError(
            f"Cannot suspend subscription in status {subscription.status.value}"
        )

    # Check for existing active lock with same reason (idempotent)
    existing = db.scalars(
        select(EnforcementLock).where(
            EnforcementLock.subscription_id == subscription.id,
            EnforcementLock.reason == reason,
            EnforcementLock.is_active.is_(True),
        )
    ).first()
    if existing:
        logger.info(
            "Duplicate lock skipped: subscription=%s reason=%s source=%s existing=%s",
            subscription_id,
            reason.value,
            source,
            existing.id,
        )
        return existing

    # Create the enforcement lock
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        reason=reason,
        source=source,
        is_active=True,
        notes=notes,
    )
    db.add(lock)

    # Transition status if not already suspended
    status_changed = False
    if not was_already_suspended:
        subscription.status = SubscriptionStatus.suspended
        status_changed = True

    db.flush()

    if emit:
        if status_changed:
            emit_event(
                db,
                EventType.subscription_suspended,
                {
                    "subscription_id": str(subscription.id),
                    "reason": reason.value,
                    "source": source,
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )
        # Always emit lock_created, even if status didn't change
        emit_event(
            db,
            EventType.enforcement_lock_created,
            {
                "lock_id": str(lock.id),
                "subscription_id": str(subscription.id),
                "reason": reason.value,
                "source": source,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

    compute_account_status(db, str(subscription.subscriber_id))

    logger.info(
        "Enforcement lock created: subscription=%s reason=%s source=%s "
        "(status_changed=%s)",
        subscription_id,
        reason.value,
        source,
        status_changed,
    )
    return lock


def restore_subscription(
    db: Session,
    subscription_id: str,
    trigger: str,
    resolved_by: str,
    *,
    reason: EnforcementReason | None = None,
    notes: str | None = None,
    emit: bool = True,
) -> bool:
    """Resolve enforcement locks and restore if no active locks remain.

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        trigger: The type of restorer (e.g. ``"payment"``, ``"admin"``).
        resolved_by: Who/what resolved this (e.g. ``"payment:{id}"``).
        reason: If set, only resolve locks with this specific reason.
            If None, resolves all locks that the trigger is allowed to clear.
        notes: Optional resolution notes.
        emit: Whether to emit events.

    Returns:
        True if the subscription was actually restored to active.
    """
    # Lock the subscription row to prevent concurrent restore races
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status not in SUSPENDED_EQUIVALENT:
        logger.warning(
            "restore_subscription called but subscription %s is %s, not suspended",
            subscription_id,
            subscription.status.value,
        )
        return False

    resolved_count, remaining = resolve_locks_for_trigger(
        db,
        subscription,
        trigger=trigger,
        resolved_by=resolved_by,
        reason=reason,
        notes=notes,
        emit=emit,
    )

    if resolved_count == 0:
        logger.warning(
            "No locks resolved for subscription %s with trigger %r "
            "(active_locks=%d, trigger not authorized)",
            subscription_id,
            trigger,
            len(get_active_locks(db, subscription_id=str(subscription.id))),
        )
        return False

    restored = False
    if remaining is None:
        subscription.status = SubscriptionStatus.active
        db.flush()
        restored = True

        if emit:
            emit_event(
                db,
                EventType.subscription_resumed,
                {
                    "subscription_id": str(subscription.id),
                    "trigger": trigger,
                    "resolved_by": resolved_by,
                    "offer_name": subscription.offer.name
                    if subscription.offer
                    else None,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

        logger.info(
            "Subscription %s restored to active (trigger=%s resolved_by=%s)",
            subscription_id,
            trigger,
            resolved_by,
        )
    else:
        logger.info(
            "Subscription %s still has active locks after resolving %d (trigger=%s)",
            subscription_id,
            resolved_count,
            trigger,
        )

    compute_account_status(db, str(subscription.subscriber_id))
    return restored


def activate_subscription(
    db: Session,
    subscription_id: str,
    *,
    start_at: datetime | None = None,
    emit: bool = True,
) -> None:
    """Transition subscription from pending to active.

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        start_at: Override start date. Defaults to now.
        emit: Whether to emit events.

    Raises:
        ValueError: If the subscription is not in pending status.
    """
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status != SubscriptionStatus.pending:
        raise ValueError(
            f"Cannot activate subscription in status {subscription.status.value}"
        )

    subscription.status = SubscriptionStatus.active
    if start_at and not subscription.start_at:
        subscription.start_at = start_at
    elif not subscription.start_at:
        subscription.start_at = datetime.now(UTC)

    db.flush()

    if emit:
        emit_event(
            db,
            EventType.subscription_activated,
            {
                "subscription_id": str(subscription.id),
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

    compute_account_status(db, str(subscription.subscriber_id))

    logger.info("Subscription %s activated", subscription_id)


def expire_subscription(
    db: Session,
    subscription_id: str,
    *,
    emit: bool = True,
) -> None:
    """Transition subscription to expired (terminal state).

    Resolves all active enforcement locks since they no longer apply.

    Raises:
        ValueError: If the subscription is already in a terminal state.
    """
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status in _TERMINAL:
        raise ValueError(
            f"Cannot expire subscription already in {subscription.status.value}"
        )

    resolved_count = resolve_all_locks(db, subscription, "expired")

    subscription.status = SubscriptionStatus.expired
    db.flush()

    if emit:
        emit_event(
            db,
            EventType.subscription_expired,
            {
                "subscription_id": str(subscription.id),
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

    compute_account_status(db, str(subscription.subscriber_id))

    logger.info(
        "Subscription %s expired (locks_resolved=%d)", subscription_id, resolved_count
    )


def cancel_subscription(
    db: Session,
    subscription_id: str,
    cancel_reason: str,
    source: str,
    *,
    emit: bool = True,
) -> None:
    """Transition subscription to canceled (terminal state).

    Args:
        db: Database session.
        subscription_id: Subscription UUID.
        cancel_reason: Cancellation reason (stored on subscription).
        source: Who/what canceled this.
        emit: Whether to emit events.

    Raises:
        ValueError: If subscription is already canceled.
    """
    subscription = db.get(Subscription, subscription_id)
    if not subscription:
        raise ValueError(f"Subscription {subscription_id} not found")

    if subscription.status == SubscriptionStatus.canceled:
        raise ValueError("Subscription is already canceled")

    resolved_count = resolve_all_locks(db, subscription, "canceled")

    subscription.status = SubscriptionStatus.canceled
    subscription.canceled_at = datetime.now(UTC)
    subscription.cancel_reason = cancel_reason
    db.flush()

    # Generate credit note for unused portion of the billing period.
    # Use a savepoint so a credit note failure doesn't corrupt the
    # cancel transaction (the cancellation itself is already flushed).
    try:
        from app.services.billing_automation import generate_cancellation_credit

        db.begin_nested()  # savepoint
        generate_cancellation_credit(db, subscription)
    except Exception as exc:
        db.rollback()  # rolls back to savepoint only
        logger.warning(
            "Cancellation credit generation failed for subscription %s: %s",
            subscription_id,
            exc,
        )

    if emit:
        emit_event(
            db,
            EventType.subscription_canceled,
            {
                "subscription_id": str(subscription.id),
                "cancel_reason": cancel_reason,
                "source": source,
                "offer_name": subscription.offer.name if subscription.offer else None,
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )

    compute_account_status(db, str(subscription.subscriber_id))

    logger.info(
        "Subscription %s canceled (reason=%s source=%s locks_resolved=%d)",
        subscription_id,
        cancel_reason,
        source,
        resolved_count,
    )


# ---------------------------------------------------------------------------
# Derived account status
# ---------------------------------------------------------------------------


def compute_account_status(db: Session, subscriber_id: str) -> SubscriberStatus:
    """Derive subscriber status from subscription states.

    Priority order:
      1. Any subscription active → active
      2. Any subscription pending → new
      3. Any subscription suspended/blocked/stopped → suspended
      4. All terminal (canceled/expired/disabled/hidden/archived) → canceled
      5. No subscriptions → new

    Updates ``subscriber.status`` and flushes.
    """
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        logger.error("compute_account_status: subscriber %s not found", subscriber_id)
        raise ValueError(f"Subscriber {subscriber_id} not found")

    subs = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id == subscriber.id)
        ).all()
    )

    if not subs:
        new_status = SubscriberStatus.new
    elif any(s.status == SubscriptionStatus.active for s in subs):
        new_status = SubscriberStatus.active
    elif any(s.status in SUSPENDED_EQUIVALENT for s in subs):
        new_status = SubscriberStatus.suspended
    elif any(s.status == SubscriptionStatus.pending for s in subs):
        new_status = SubscriberStatus.new
    else:
        # All terminal (canceled, expired, disabled, hidden, archived)
        new_status = SubscriberStatus.canceled

    if subscriber.status != new_status:
        logger.info(
            "Account status derived: subscriber=%s %s → %s",
            subscriber_id,
            subscriber.status.value,
            new_status.value,
        )
        subscriber.status = new_status

    # Sync is_active flag with derived status.
    # Suspended subscribers remain is_active=True so they can still
    # log into the customer portal to view invoices and make payments.
    should_be_active = new_status in {
        SubscriberStatus.active,
        SubscriberStatus.new,
        SubscriberStatus.suspended,
    }
    if subscriber.is_active != should_be_active:
        subscriber.is_active = should_be_active

    db.flush()

    return new_status


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_active_locks(
    db: Session,
    *,
    subscription_id: str | None = None,
    subscriber_id: str | None = None,
) -> list[EnforcementLock]:
    """Query active enforcement locks for a subscription or subscriber."""
    stmt = select(EnforcementLock).where(EnforcementLock.is_active.is_(True))
    if subscription_id:
        stmt = stmt.where(EnforcementLock.subscription_id == subscription_id)
    if subscriber_id:
        stmt = stmt.where(EnforcementLock.subscriber_id == subscriber_id)
    return list(db.scalars(stmt).all())


def has_active_lock(
    db: Session,
    subscription_id: str,
    reason: EnforcementReason | None = None,
) -> bool:
    """Check if a subscription has any (or specific reason) active lock."""
    stmt = select(EnforcementLock.id).where(
        EnforcementLock.subscription_id == subscription_id,
        EnforcementLock.is_active.is_(True),
    )
    if reason is not None:
        stmt = stmt.where(EnforcementLock.reason == reason)
    return db.scalars(stmt).first() is not None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def resolve_locks_for_trigger(
    db: Session,
    subscription: Subscription,
    *,
    trigger: str,
    resolved_by: str,
    reason: EnforcementReason | None = None,
    notes: str | None = None,
    emit: bool = True,
) -> tuple[int, EnforcementLock | None]:
    """Resolve the active locks a trigger is authorized to clear.

    Returns a tuple of ``(resolved_count, remaining_active_lock)`` after the
    resolution attempt.
    """
    stmt = select(EnforcementLock).where(
        EnforcementLock.subscription_id == subscription.id,
        EnforcementLock.is_active.is_(True),
    )
    if reason is not None:
        stmt = stmt.where(EnforcementLock.reason == reason)

    active_locks = list(db.scalars(stmt).all())
    now = datetime.now(UTC)
    resolved_count = 0

    for lock in active_locks:
        allowed = ALLOWED_RESTORERS.get(lock.reason, set())
        if trigger not in allowed:
            logger.info(
                "Trigger %r not allowed to resolve %s lock (lock=%s)",
                trigger,
                lock.reason.value,
                lock.id,
            )
            continue

        lock.is_active = False
        lock.resolved_at = now
        lock.resolved_by = resolved_by
        if notes:
            existing_notes = lock.notes or ""
            lock.notes = (
                f"{existing_notes}\nResolved: {notes}" if existing_notes else notes
            )
        resolved_count += 1

        if emit:
            emit_event(
                db,
                EventType.enforcement_lock_resolved,
                {
                    "lock_id": str(lock.id),
                    "subscription_id": str(subscription.id),
                    "reason": lock.reason.value,
                    "trigger": trigger,
                    "resolved_by": resolved_by,
                },
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
            )

    if resolved_count:
        db.flush()

    remaining = db.scalars(
        select(EnforcementLock).where(
            EnforcementLock.subscription_id == subscription.id,
            EnforcementLock.is_active.is_(True),
        )
    ).first()
    return resolved_count, remaining


def resolve_all_locks(db: Session, subscription: Subscription, resolved_by: str) -> int:
    """Resolve all active locks on a subscription (for terminal transitions).

    Used internally by ``expire_subscription`` and ``cancel_subscription``,
    and also by the catalog module when handling status transitions that
    have already been committed.
    """
    locks = list(
        db.scalars(
            select(EnforcementLock).where(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.is_active.is_(True),
            )
        ).all()
    )
    now = datetime.now(UTC)
    for lock in locks:
        lock.is_active = False
        lock.resolved_at = now
        lock.resolved_by = resolved_by
    if locks:
        db.flush()
    return len(locks)
