"""Reconcile stale ``overdue`` enforcement locks (dry-run first).

An ``overdue`` lock should be resolved when its debt is settled (the
restore-on-payment path). A lock that is still active while the account owes NO
overdue debt is stale drift — it keeps a paid-up service suspended. (Surfaced by
the prepaid-scope repair: 4 of those postpaid subs were also held by a stale
overdue lock.)

Conservative + audit-first, mirroring ``prepaid_scope_repair``:

* Cohort = suspended-equivalent subs with an ACTIVE ``overdue`` lock whose
  account currently has NO overdue debt.
* Resolves only the ``overdue`` lock (trigger=admin). ``restore_subscription``
  reactivates iff no other active lock remains; a sub also held by another lock
  (e.g. a wrongful ``prepaid`` lock — run ``prepaid_scope_repair`` too) has its
  overdue lock cleared but stays suspended, and is reported.
* Dry-run by default; nothing is written unless ``apply=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.services.account_lifecycle import (
    SUSPENDED_EQUIVALENT,
    get_active_locks,
    restore_subscription,
)
from app.services.collections import has_overdue_balance

_TRIGGER = "admin"
_RESOLVED_BY = "stale_overdue_lock_reconcile"
_NOTES = "Cleared stale overdue lock (account has no overdue debt)"


@dataclass
class ReconcileItem:
    subscription_id: str
    subscriber_id: str
    other_active_locks: list[str]  # active lock reasons besides 'overdue'
    action: str = (
        ""  # would_restore|would_clear_lock_only|restored|lock_cleared_not_restored
    )
    detail: str = ""


@dataclass
class ReconcileResult:
    applied: bool
    candidates: int
    restored: int = 0
    lock_cleared_only: int = 0
    items: list[ReconcileItem] = field(default_factory=list)


def find_candidates(
    db: Session,
    sub_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[Subscription]:
    """Suspended-equivalent subs carrying an active overdue lock."""
    q = (
        db.query(Subscription)
        .join(EnforcementLock, EnforcementLock.subscription_id == Subscription.id)
        .filter(Subscription.status.in_(list(SUSPENDED_EQUIVALENT)))
        .filter(EnforcementLock.is_active.is_(True))
        .filter(EnforcementLock.reason == EnforcementReason.overdue)
        .distinct()
    )
    if sub_ids:
        q = q.filter(Subscription.id.in_(sub_ids))
    q = q.order_by(Subscription.id)
    if limit:
        q = q.limit(limit)
    return q.all()


def reconcile(
    db: Session,
    *,
    apply: bool = False,
    sub_ids: list[str] | None = None,
    limit: int | None = None,
) -> ReconcileResult:
    """Clear stale overdue locks (account has no overdue debt). Dry-run default."""
    candidates = find_candidates(db, sub_ids=sub_ids, limit=limit)
    result = ReconcileResult(applied=apply, candidates=0)

    for sub in candidates:
        sid = str(sub.id)
        # Only STALE locks: skip any account that genuinely owes overdue debt.
        if has_overdue_balance(db, str(sub.subscriber_id)):
            continue
        result.candidates += 1
        other = sorted(
            {
                lock.reason.value
                for lock in get_active_locks(db, subscription_id=sid)
                if lock.reason != EnforcementReason.overdue
            }
        )
        item = ReconcileItem(
            subscription_id=sid,
            subscriber_id=str(sub.subscriber_id),
            other_active_locks=other,
        )
        will_reactivate = not other

        if not apply:
            item.action = (
                "would_restore" if will_reactivate else "would_clear_lock_only"
            )
            item.detail = (
                "would clear stale overdue lock and reactivate"
                if will_reactivate
                else f"would clear stale overdue lock; stays suspended (locks: {other})"
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
            reason=EnforcementReason.overdue,
            notes=_NOTES,
        )
        if restored:
            item.action = "restored"
            item.detail = "stale overdue lock cleared; reactivated"
            result.restored += 1
        else:
            item.action = "lock_cleared_not_restored"
            item.detail = (
                f"stale overdue lock cleared; stays suspended (locks: {other})"
            )
            result.lock_cleared_only += 1
        result.items.append(item)

    if apply:
        db.commit()
    return result
