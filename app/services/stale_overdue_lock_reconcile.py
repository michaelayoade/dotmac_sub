"""Reconcile stale ``overdue`` enforcement locks (dry-run first).

An ``overdue`` lock should be resolved when its debt is settled (the
restore-on-payment path). A lock that is still active while the account owes NO
overdue debt is stale drift — it keeps a paid-up service suspended.

Conservative + audit-first:

* Cohort = suspended-equivalent subs with an ACTIVE ``overdue`` lock whose
  account currently has NO overdue debt.
* Resolves only the ``overdue`` lock (trigger=admin). ``restore_subscription``
  reactivates iff no other active lock remains; a sub also held by another lock
  has its overdue lock cleared but stays suspended, and is reported.
* Dry-run by default; nothing is written unless ``apply=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber
from app.services.account_lifecycle import (
    SUSPENDED_EQUIVALENT,
    compute_account_status,
    get_active_locks,
    reactivation_blocked_by_active_login,
    resolve_locks_for_trigger,
    restore_subscription,
)
from app.services import settings_spec
from app.services.collections import get_available_balance, has_overdue_balance

_TRIGGER = "admin"
_RESOLVED_BY = "stale_overdue_lock_reconcile"
_NOTES = "Cleared stale overdue lock (account has no overdue debt)"


@dataclass
class ReconcileItem:
    subscription_id: str
    subscriber_id: str
    other_active_locks: list[str]  # active lock reasons besides 'overdue'
    available_balance: str | None = None
    min_balance: str | None = None
    action: str = (
        ""  # would_restore|would_clear_lock_only|restored|lock_cleared_not_restored|lock_cleared
    )
    detail: str = ""


@dataclass
class ReconcileResult:
    applied: bool
    candidates: int
    restored: int = 0
    lock_cleared_only: int = 0
    skipped: int = 0
    items: list[ReconcileItem] = field(default_factory=list)


def _minimum_required_balance(db: Session, subscriber_id) -> Decimal:
    account = db.get(Subscriber, subscriber_id)
    if account is not None and account.min_balance is not None:
        return Decimal(str(account.min_balance))
    default = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_default_min_balance"
    )
    return Decimal(str(default)) if default is not None else Decimal("0.00")


def _ledger_covers_account(db: Session, subscriber_id) -> tuple[bool, Decimal, Decimal]:
    available = Decimal(str(get_available_balance(db, str(subscriber_id))))
    threshold = _minimum_required_balance(db, subscriber_id)
    return available >= threshold, available, threshold


def find_candidates(
    db: Session,
    sub_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[Subscription]:
    """Subscriptions carrying an active overdue lock."""
    q = (
        db.query(Subscription)
        .join(EnforcementLock, EnforcementLock.subscription_id == Subscription.id)
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
    restore_ledger_covered: bool = False,
) -> ReconcileResult:
    """Clear stale/covered overdue locks. Dry-run default.

    Default mode restores only accounts with no overdue debt. With
    ``restore_ledger_covered=True``, accounts that still have overdue invoice
    rows are also eligible when local available balance covers their minimum
    required balance. That is the production repair for pre-gate dunning locks:
    it resolves the enforcement lock without changing invoices or money.
    """
    candidates = find_candidates(db, sub_ids=sub_ids, limit=limit)
    result = ReconcileResult(applied=apply, candidates=0)

    for sub in candidates:
        sid = str(sub.id)
        has_overdue = has_overdue_balance(db, str(sub.subscriber_id))
        covered = False
        available: Decimal | None = None
        threshold: Decimal | None = None
        if has_overdue and restore_ledger_covered:
            covered, available, threshold = _ledger_covers_account(
                db, sub.subscriber_id
            )
        # Only stale/covered locks: skip any account that genuinely owes debt
        # and is not covered by local ledger credit.
        if has_overdue and not covered:
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
            available_balance=str(available) if available is not None else None,
            min_balance=str(threshold) if threshold is not None else None,
        )
        subscription_is_suspended = sub.status in SUSPENDED_EQUIVALENT
        will_reactivate = subscription_is_suspended and not other

        # Superseded duplicate (subscriber already active on this login) — don't
        # flip it back to active (active-login uniqueness). Skip.
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
            if will_reactivate:
                item.action = "would_restore"
                item.detail = "would clear stale/covered overdue lock and reactivate"
            elif subscription_is_suspended:
                item.action = "would_clear_lock_only"
                item.detail = (
                    "would clear stale/covered overdue lock; "
                    f"stays suspended (locks: {other})"
                )
            else:
                item.action = "would_clear_lock"
                item.detail = "would clear stale/covered overdue lock"
            if will_reactivate:
                result.restored += 1
            else:
                result.lock_cleared_only += 1
            result.items.append(item)
            continue

        if subscription_is_suspended:
            restored = restore_subscription(
                db,
                sid,
                trigger=_TRIGGER,
                resolved_by=_RESOLVED_BY,
                reason=EnforcementReason.overdue,
                notes=_NOTES,
            )
        else:
            resolved_count, _remaining = resolve_locks_for_trigger(
                db,
                sub,
                trigger=_TRIGGER,
                resolved_by=_RESOLVED_BY,
                reason=EnforcementReason.overdue,
                notes=_NOTES,
            )
            if resolved_count:
                compute_account_status(db, str(sub.subscriber_id))
            restored = False
        if restored:
            item.action = "restored"
            item.detail = "stale/covered overdue lock cleared; reactivated"
            result.restored += 1
        elif not subscription_is_suspended:
            item.action = "lock_cleared"
            item.detail = "stale/covered overdue lock cleared"
            result.lock_cleared_only += 1
        else:
            item.action = "lock_cleared_not_restored"
            item.detail = (
                f"stale/covered overdue lock cleared; stays suspended (locks: {other})"
            )
            result.lock_cleared_only += 1
        result.items.append(item)

    if apply:
        db.commit()
    return result
