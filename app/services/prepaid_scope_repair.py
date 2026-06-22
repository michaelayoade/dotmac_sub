"""Repair postpaid services wrongly suspended by prepaid balance enforcement.

Companion to the PrepaidEnforcement scope fix
(``_suspend_account(only_billing_mode=...)``): that prevents *recurrence*; this
clears the already-applied wrongful ``prepaid`` enforcement lock from postpaid
subscriptions. A postpaid service lapses only via dunning, so a ``prepaid`` lock
on a postpaid subscription is never valid.

Conservative by design:

* Cohort = postpaid subs in a suspended-equivalent status with an ACTIVE
  ``prepaid`` lock.
* Accounts that currently owe overdue debt are SKIPPED entirely (they are
  legitimately suspendable via dunning — not this tool's concern).
* Only the ``prepaid`` lock is resolved. ``restore_subscription`` reactivates
  iff no other active lock remains, so a sub still held by e.g. a stale
  ``overdue`` lock has its prepaid lock cleared but stays suspended — reported
  for the dunning/lock-drift flow rather than force-activated here.
* Dry-run by default; nothing is written unless ``apply=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.services.account_lifecycle import (
    SUSPENDED_EQUIVALENT,
    get_active_locks,
    reactivation_blocked_by_active_login,
    restore_subscription,
)
from app.services.collections import has_overdue_balance

_TRIGGER = "admin"
_RESOLVED_BY = "prepaid_scope_repair"
_NOTES = "Cleared wrongful prepaid lock on postpaid service (prepaid scope repair)"


@dataclass
class RepairItem:
    subscription_id: str
    subscriber_id: str
    other_active_locks: list[str]  # active lock reasons besides 'prepaid'
    has_overdue_debt: bool
    # would_restore|would_clear_lock_only|restored|lock_cleared_not_restored|skipped
    action: str = ""
    detail: str = ""


@dataclass
class RepairResult:
    applied: bool
    candidates: int
    restored: int = 0
    lock_cleared_only: int = 0
    skipped: int = 0
    items: list[RepairItem] = field(default_factory=list)


def find_candidates(
    db: Session,
    sub_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[Subscription]:
    """Postpaid, suspended-equivalent subs carrying an active prepaid lock."""
    q = (
        db.query(Subscription)
        .join(EnforcementLock, EnforcementLock.subscription_id == Subscription.id)
        .filter(Subscription.billing_mode == BillingMode.postpaid)
        .filter(Subscription.status.in_(list(SUSPENDED_EQUIVALENT)))
        .filter(EnforcementLock.is_active.is_(True))
        .filter(EnforcementLock.reason == EnforcementReason.prepaid)
        .distinct()
    )
    if sub_ids:
        q = q.filter(Subscription.id.in_(sub_ids))
    q = q.order_by(Subscription.id)
    if limit:
        q = q.limit(limit)
    return q.all()


def repair(
    db: Session,
    *,
    apply: bool = False,
    sub_ids: list[str] | None = None,
    limit: int | None = None,
) -> RepairResult:
    """Clear wrongful prepaid locks from postpaid subs (dry-run unless apply)."""
    candidates = find_candidates(db, sub_ids=sub_ids, limit=limit)
    result = RepairResult(applied=apply, candidates=len(candidates))

    for sub in candidates:
        sid = str(sub.id)
        overdue = has_overdue_balance(db, str(sub.subscriber_id))
        other = sorted(
            {
                lock.reason.value
                for lock in get_active_locks(db, subscription_id=sid)
                if lock.reason != EnforcementReason.prepaid
            }
        )
        item = RepairItem(
            subscription_id=sid,
            subscriber_id=str(sub.subscriber_id),
            other_active_locks=other,
            has_overdue_debt=overdue,
        )

        # Safety gate: only touch the genuinely-wrongful set (no overdue debt).
        if overdue:
            item.action = "skipped"
            item.detail = "account has overdue debt; leave to dunning"
            result.skipped += 1
            result.items.append(item)
            continue

        will_reactivate = not other

        # A suspended sub whose subscriber already has an active sub on the same
        # login is a superseded duplicate — reactivating it would violate the
        # active-login uniqueness index. Skip (don't clear the lock either).
        if will_reactivate and reactivation_blocked_by_active_login(db, sub):
            item.action = "skipped"
            item.detail = (
                "subscriber already has an active subscription on this login "
                "(superseded duplicate); review manually"
            )
            result.skipped += 1
            result.items.append(item)
            continue

        if not apply:
            item.action = (
                "would_restore" if will_reactivate else "would_clear_lock_only"
            )
            item.detail = (
                "would clear prepaid lock and reactivate"
                if will_reactivate
                else f"would clear prepaid lock; stays suspended (locks: {other})"
            )
            if will_reactivate:
                result.restored += 1
            else:
                result.lock_cleared_only += 1
            result.items.append(item)
            continue

        restored = restore_subscription(
            db,
            sid,
            trigger=_TRIGGER,
            resolved_by=_RESOLVED_BY,
            reason=EnforcementReason.prepaid,
            notes=_NOTES,
        )
        if restored:
            item.action = "restored"
            item.detail = "prepaid lock cleared; reactivated"
            result.restored += 1
        else:
            item.action = "lock_cleared_not_restored"
            item.detail = f"prepaid lock cleared; stays suspended (locks: {other})"
            result.lock_cleared_only += 1
        result.items.append(item)

    if apply:
        db.commit()
    return result
