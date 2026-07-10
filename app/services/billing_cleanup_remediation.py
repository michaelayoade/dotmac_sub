"""Gated remediation for billing cleanup audit CSVs."""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
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
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.collections import has_overdue_balance
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

_RESOLVED_BY = "billing_cleanup_remediation"
_LOCK_NOTES = "Cleared stale overdue lock from billing cleanup audit"
ANCHOR_REPAIR_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.paid,
    InvoiceStatus.overdue,
)


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


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    return _as_utc(left) == _as_utc(right)


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


def discover_invoice_anchor_rows(
    db: Session, *, run_at: datetime | None = None
) -> list[dict[str, str]]:
    """Find subscriptions whose anchor is behind active invoice-line coverage."""
    effective_run_at = run_at or datetime.now(UTC)
    rows = (
        db.query(Subscription, Subscriber, CatalogOffer, Invoice, InvoiceLine)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .outerjoin(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
        .filter(Subscription.next_billing_at.isnot(None))
        .filter(Subscription.next_billing_at <= effective_run_at)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.is_proforma.is_(False))
        .filter(Invoice.status.in_(ANCHOR_REPAIR_STATUSES))
        .filter(Invoice.billing_period_end.isnot(None))
        .filter(Invoice.billing_period_end > Subscription.next_billing_at)
        .order_by(
            Subscription.id.asc(),
            Invoice.billing_period_end.desc(),
            Invoice.created_at.desc(),
            Invoice.id.desc(),
        )
        .all()
    )

    latest_by_subscription: dict[str, dict[str, str]] = {}
    for subscription, account, offer, invoice, _line in rows:
        subscription_id = str(subscription.id)
        if subscription_id in latest_by_subscription:
            continue
        account_name = getattr(account, "display_name", "") or ""
        latest_by_subscription[subscription_id] = {
            "issue": "invoice_anchor_behind_paid_through",
            "account_id": str(account.id),
            "account_name": account_name,
            "account_status": account.status.value,
            "subscription_id": subscription_id,
            "subscription_status": subscription.status.value,
            "subscription_mode": subscription.billing_mode.value,
            "offer_id": str(subscription.offer_id),
            "offer_name": offer.name if offer else "",
            "current_next_billing_at": _iso(subscription.next_billing_at),
            "paid_through": _iso(invoice.billing_period_end),
            "latest_invoice_id": str(invoice.id),
            "latest_invoice_number": invoice.invoice_number or "",
            "latest_invoice_status": invoice.status.value,
            "latest_invoice_period_start": _iso(invoice.billing_period_start),
            "latest_invoice_period_end": _iso(invoice.billing_period_end),
        }
    return list(latest_by_subscription.values())


def plan_invoice_anchor_row(db: Session, row: dict[str, str]) -> dict[str, Any]:
    action = "advance_invoice_next_billing_at"
    if row.get("issue") not in {
        "",
        None,
        "invoice_anchor_behind_paid_through",
    }:
        return _refuse(action, row, "not_invoice_anchor_behind_paid_through")
    subscription_id = (row.get("subscription_id") or "").strip()
    target = _parse_datetime(row.get("paid_through"))
    reviewed_current = _parse_datetime(row.get("current_next_billing_at"))
    if not subscription_id:
        return _refuse(action, row, "missing_subscription_id")
    if target is None:
        return _refuse(action, row, "bad_paid_through")
    target_utc = _as_utc(target)
    if target_utc is None:
        return _refuse(action, row, "bad_paid_through")
    try:
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
    except Exception:
        return _refuse(action, row, "bad_subscription_id")
    if subscription is None:
        return _refuse(action, row, "subscription_missing")
    if row.get("account_id") and str(subscription.subscriber_id) != row["account_id"]:
        return _refuse(action, row, "account_id_changed")
    if (
        row.get("subscription_mode")
        and subscription.billing_mode.value != row["subscription_mode"]
    ):
        return _refuse(action, row, "subscription_mode_changed")
    if (
        row.get("subscription_status")
        and subscription.status.value != row["subscription_status"]
    ):
        return _refuse(action, row, "subscription_status_changed")
    current = _as_utc(subscription.next_billing_at)
    if reviewed_current is not None and not _same_datetime(current, reviewed_current):
        return _refuse(action, row, "next_billing_at_changed_since_audit")
    if current is not None and current >= target_utc:
        return {
            "action": action,
            "decision": "skip",
            "reason": "already_at_or_after_paid_through",
            "subscription_id": str(subscription.id),
            "before": {"next_billing_at": _iso(subscription.next_billing_at)},
        }

    invoice_id = (row.get("latest_invoice_id") or "").strip()
    if invoice_id:
        try:
            invoice = db.get(Invoice, coerce_uuid(invoice_id))
        except Exception:
            return _refuse(action, row, "bad_latest_invoice_id")
        if invoice is None:
            return _refuse(action, row, "latest_invoice_missing")
        if (
            not invoice.is_active
            or invoice.is_proforma
            or invoice.status not in ANCHOR_REPAIR_STATUSES
            or not _same_datetime(invoice.billing_period_end, target)
        ):
            return _refuse(action, row, "latest_invoice_changed_since_audit")
        active_line = (
            db.query(InvoiceLine.id)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .filter(InvoiceLine.is_active.is_(True))
            .first()
        )
        if active_line is None:
            return _refuse(action, row, "latest_invoice_line_missing")

    return {
        "action": action,
        "decision": "apply",
        "subscription_id": str(subscription.id),
        "subscriber_id": str(subscription.subscriber_id),
        "target_next_billing_at": target_utc.isoformat(),
        "latest_invoice_id": invoice_id,
        "before": {"next_billing_at": _iso(subscription.next_billing_at)},
    }


def plan_cleanup_remediation(
    db: Session,
    *,
    stale_lock_rows: list[dict[str, str]] | None = None,
    anchor_rows: list[dict[str, str]] | None = None,
    mode_rows: list[dict[str, str]] | None = None,
    invoice_anchor_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    items.extend(plan_stale_overdue_lock_row(db, row) for row in stale_lock_rows or [])
    items.extend(plan_anchor_row(db, row) for row in anchor_rows or [])
    items.extend(plan_account_mode_row(db, row) for row in mode_rows or [])
    items.extend(plan_invoice_anchor_row(db, row) for row in invoice_anchor_rows or [])

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
    if item["action"] == "advance_invoice_next_billing_at":
        subscription = db.get(Subscription, coerce_uuid(item["subscription_id"]))
        if subscription is None:
            raise ValueError(f"subscription missing: {item['subscription_id']}")
        target = _parse_datetime(item["target_next_billing_at"])
        if target is None:
            raise ValueError(f"bad target_next_billing_at: {item['subscription_id']}")
        subscription.next_billing_at = target
        db.flush()
        return {"next_billing_at": _iso(subscription.next_billing_at)}
    raise ValueError(f"unknown cleanup action {item['action']}")
