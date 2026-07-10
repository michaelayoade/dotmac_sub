"""Gated remediation for billing cleanup audit CSVs."""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
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
from app.services.collections import has_overdue_balance
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

_RESOLVED_BY = "billing_cleanup_remediation"
_LOCK_NOTES = "Cleared stale overdue lock from billing cleanup audit"


def load_cleanup_csv(path: str) -> list[dict[str, str]]:
    with open(path, newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _refuse(action: str, row: dict[str, str], reason: str) -> dict[str, Any]:
    return {
        "action": action,
        "decision": "refuse",
        "reason": reason,
        "row": row,
    }


def plan_stale_overdue_lock_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "resolve_stale_overdue_lock"
    lock_id = (row.get("lock_id") or "").strip()
    if not lock_id:
        return _refuse(action, row, "missing_lock_id")
    try:
        lock = db.get(EnforcementLock, coerce_uuid(lock_id))
    except Exception:
        return _refuse(action, row, "bad_lock_id")
    if lock is None:
        return _refuse(action, row, "lock_missing")
    if not lock.is_active:
        return _refuse(action, row, "lock_already_inactive")
    if lock.reason != EnforcementReason.overdue:
        return _refuse(action, row, "lock_not_overdue")
    if row.get("account_id") and str(lock.subscriber_id) != row["account_id"]:
        return _refuse(action, row, "account_id_changed")
    if (
        row.get("subscription_id")
        and str(lock.subscription_id) != row["subscription_id"]
    ):
        return _refuse(action, row, "subscription_id_changed")
    if row.get("source") and lock.source != row["source"]:
        return _refuse(action, row, "lock_source_changed")
    if has_overdue_balance(db, str(lock.subscriber_id)):
        return _refuse(action, row, "account_has_collectible_overdue_ar")

    subscription = db.get(Subscription, lock.subscription_id)
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    other_locks = sorted(
        {
            active.reason.value
            for active in get_active_locks(db, subscription_id=str(subscription.id))
            if active.id != lock.id and active.reason != EnforcementReason.overdue
        }
    )
    would_restore = subscription.status in SUSPENDED_EQUIVALENT and not other_locks
    if would_restore and reactivation_blocked_by_active_login(db, subscription):
        return _refuse(action, row, "reactivation_blocked_by_active_login")

    return {
        "action": action,
        "decision": "apply",
        "lock_id": str(lock.id),
        "subscriber_id": str(lock.subscriber_id),
        "subscription_id": str(lock.subscription_id),
        "would_restore": would_restore,
        "other_active_locks": other_locks,
        "before": {
            "lock_is_active": lock.is_active,
            "lock_reason": lock.reason.value,
            "lock_source": lock.source,
            "subscription_status": subscription.status.value,
        },
    }


def plan_anchor_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "advance_prepaid_next_billing_at"
    subscription_id = (row.get("subscription_id") or "").strip()
    paid_through = _parse_datetime(row.get("paid_through"))
    if not subscription_id:
        return _refuse(action, row, "missing_subscription_id")
    if paid_through is None:
        return _refuse(action, row, "bad_paid_through")
    try:
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
    except Exception:
        return _refuse(action, row, "bad_subscription_id")
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if subscription.billing_mode != BillingMode.prepaid:
        return _refuse(action, row, "subscription_not_prepaid")
    if row.get("account_id") and str(subscription.subscriber_id) != row["account_id"]:
        return _refuse(action, row, "account_id_changed")
    current = _as_utc(subscription.next_billing_at)
    target = _as_utc(paid_through)
    if target is None:
        return _refuse(action, row, "bad_paid_through")
    if current is not None and current >= target:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_at_or_after_paid_through",
            "subscription_id": str(subscription.id),
            "before": {"next_billing_at": _iso(subscription.next_billing_at)},
        }
    reviewed_current = _parse_datetime(row.get("current_next_billing_at"))
    if reviewed_current is not None and current != _as_utc(reviewed_current):
        return _refuse(action, row, "next_billing_at_changed_since_audit")

    return {
        "action": action,
        "decision": "apply",
        "subscription_id": str(subscription.id),
        "subscriber_id": str(subscription.subscriber_id),
        "target_next_billing_at": target.isoformat(),
        "before": {"next_billing_at": _iso(subscription.next_billing_at)},
    }


def plan_account_mode_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "align_account_billing_mode"
    if row.get("issue") != "subscription_vs_account":
        return _refuse(action, row, "not_subscription_vs_account")
    subscriber_id = (row.get("subscriber_id") or "").strip()
    subscription_mode = (row.get("subscription_mode") or "").strip()
    if subscription_mode not in {mode.value for mode in BillingMode}:
        return _refuse(action, row, "bad_subscription_mode")
    if not subscriber_id:
        return _refuse(action, row, "missing_subscriber_id")
    try:
        account = db.get(Subscriber, coerce_uuid(subscriber_id))
    except Exception:
        return _refuse(action, row, "bad_subscriber_id")
    if account is None:
        return _refuse(action, row, "subscriber_missing")

    live_modes = {
        mode.value
        for (mode,) in (
            db.query(Subscription.billing_mode)
            .filter(Subscription.subscriber_id == account.id)
            .filter(
                Subscription.status.in_(
                    [
                        SubscriptionStatus.active,
                        SubscriptionStatus.pending,
                        SubscriptionStatus.suspended,
                    ]
                )
            )
            .distinct()
            .all()
        )
    }
    if live_modes != {subscription_mode}:
        return _refuse(action, row, "mixed_or_changed_live_subscription_modes")
    target_mode = BillingMode(subscription_mode)
    if account.billing_mode == target_mode:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_aligned",
            "subscriber_id": str(account.id),
            "before": {"account_billing_mode": account.billing_mode.value},
        }
    if row.get("account_mode") and account.billing_mode.value != row["account_mode"]:
        return _refuse(action, row, "account_mode_changed_since_audit")
    return {
        "action": action,
        "decision": "apply",
        "subscriber_id": str(account.id),
        "target_billing_mode": target_mode.value,
        "before": {"account_billing_mode": account.billing_mode.value},
    }


def plan_cleanup_remediation(
    db: Session,
    *,
    stale_lock_rows: list[dict[str, str]] | None = None,
    anchor_rows: list[dict[str, str]] | None = None,
    mode_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    items.extend(plan_stale_overdue_lock_row(db, row) for row in stale_lock_rows or [])
    items.extend(plan_anchor_row(db, row) for row in anchor_rows or [])
    items.extend(plan_account_mode_row(db, row) for row in mode_rows or [])

    from collections import Counter

    by_decision = Counter(item["decision"] for item in items)
    by_action = Counter(item["action"] for item in items if item["decision"] == "apply")
    return {
        "items": items,
        "counts": {
            "apply": by_decision.get("apply", 0),
            "skip": by_decision.get("skip", 0),
            "refuse": by_decision.get("refuse", 0),
            "by_action": dict(by_action),
        },
    }


def apply_cleanup_remediation(
    db: Session, plan: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    errors = 0
    for item in plan["items"]:
        if item["decision"] != "apply":
            continue
        if dry_run:
            applied.append({**item, "applied": False})
            continue
        try:
            after = _execute_item(db, item)
            db.commit()
            applied.append({**item, "applied": True, "after": after})
        except Exception:
            db.rollback()
            errors += 1
            logger.exception("billing cleanup remediation failed: %s", item)
            applied.append({**item, "applied": False, "error": True})
    return {
        "dry_run": dry_run,
        "applied": applied,
        "errors": errors,
        "applied_count": sum(1 for item in applied if item.get("applied")),
    }


def _execute_item(db: Session, item: dict[str, Any]) -> dict[str, Any]:
    if item["action"] == "resolve_stale_overdue_lock":
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        if subscription is None:
            raise ValueError(f"subscription missing: {item['subscription_id']}")
        if item["before"]["subscription_status"] in {
            status.value for status in SUSPENDED_EQUIVALENT
        }:
            restored = restore_subscription(
                db,
                item["subscription_id"],
                trigger="admin",
                resolved_by=_RESOLVED_BY,
                reason=EnforcementReason.overdue,
                notes=_LOCK_NOTES,
            )
        else:
            resolve_locks_for_trigger(
                db,
                subscription,
                trigger="admin",
                resolved_by=_RESOLVED_BY,
                reason=EnforcementReason.overdue,
                notes=_LOCK_NOTES,
            )
            compute_account_status(db, item["subscriber_id"])
            restored = False
        db.flush()
        lock = db.get(EnforcementLock, coerce_uuid(item["lock_id"]))
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        return {
            "lock_is_active": bool(lock and lock.is_active),
            "subscription_status": subscription.status.value if subscription else "",
            "restored": restored,
        }
    if item["action"] == "advance_prepaid_next_billing_at":
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        if subscription is None:
            raise ValueError(f"subscription missing: {item['subscription_id']}")
        target = _parse_datetime(item["target_next_billing_at"])
        if target is None:
            raise ValueError(f"bad target_next_billing_at: {item['subscription_id']}")
        subscription.next_billing_at = target
        db.flush()
        return {"next_billing_at": _iso(subscription.next_billing_at)}
    if item["action"] == "align_account_billing_mode":
        account = db.get(Subscriber, coerce_uuid(item["subscriber_id"]))
        if account is None:
            raise ValueError(f"subscriber missing: {item['subscriber_id']}")
        account.billing_mode = BillingMode(item["target_billing_mode"])
        db.flush()
        return {"account_billing_mode": account.billing_mode.value}
    raise ValueError(f"unknown cleanup action {item['action']}")
