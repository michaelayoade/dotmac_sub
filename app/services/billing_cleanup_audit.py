"""Consolidated dry-run audit for billing cleanup after mode/anchor drift."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber
from app.services.billing_mode_audit import find_billing_mode_inconsistencies
from app.services.billing_prepaid_overlap_repair import find_prepaid_overlap_candidates
from app.services.collections._core import has_overdue_balance

PREPAID_PHANTOM_AR_MARKER = "prepaid_phantom_ar_cleanup"

_AR_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)
_RELEVANT_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.pending,
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
)


@dataclass(frozen=True)
class BillingCleanupReport:
    prepaid_collectible_ar: list[dict[str, Any]]
    prepaid_overlaps: list[dict[str, Any]]
    stale_dunning_cases: list[dict[str, Any]]
    stale_overdue_locks: list[dict[str, Any]]
    billing_mode_drift: list[dict[str, Any]]
    next_billing_anchor_drift: list[dict[str, Any]]

    def summary(self) -> dict[str, int]:
        return {
            "prepaid_collectible_ar": len(self.prepaid_collectible_ar),
            "prepaid_overlaps": len(self.prepaid_overlaps),
            "stale_dunning_cases": len(self.stale_dunning_cases),
            "stale_overdue_locks": len(self.stale_overdue_locks),
            "billing_mode_drift": len(self.billing_mode_drift),
            "next_billing_anchor_drift": len(self.next_billing_anchor_drift),
        }


def _enum(value: object) -> str:
    if value is None:
        return ""
    return getattr(value, "value", str(value))


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _money(value: object) -> str:
    return str(Decimal(str(value or "0.00")))


def _account_name(account: Subscriber | None) -> str:
    if account is None:
        return ""
    display_name = getattr(account, "display_name", None)
    if display_name:
        return str(display_name)
    return " ".join(
        part
        for part in [
            getattr(account, "first_name", None),
            getattr(account, "last_name", None),
        ]
        if part
    )


def find_prepaid_collectible_ar(db: Session) -> list[dict[str, Any]]:
    """Open prepaid-mode invoices that should be reviewed as phantom AR."""

    open_invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(_AR_STATUSES))
        .filter(Invoice.balance_due > Decimal("0.00"))
        .order_by(Invoice.account_id.asc(), Invoice.issued_at.asc().nulls_last())
        .all()
    )
    account_ids = {invoice.account_id for invoice in open_invoices}
    if not account_ids:
        return []
    accounts = {
        account.id: account
        for account in db.query(Subscriber).filter(Subscriber.id.in_(account_ids)).all()
    }
    prepaid_subscription_account_ids = {
        row[0]
        for row in (
            db.query(Subscription.subscriber_id)
            .filter(Subscription.subscriber_id.in_(account_ids))
            .filter(Subscription.billing_mode == BillingMode.prepaid)
            .filter(Subscription.status.in_(_RELEVANT_SUBSCRIPTION_STATUSES))
            .distinct()
            .all()
        )
    }
    result: list[dict[str, Any]] = []
    for invoice in open_invoices:
        account = accounts.get(invoice.account_id)
        if account is None:
            continue
        if (
            account.billing_mode != BillingMode.prepaid
            and invoice.account_id not in prepaid_subscription_account_ids
        ):
            continue
        metadata = invoice.metadata_ or {}
        if metadata.get(PREPAID_PHANTOM_AR_MARKER):
            continue
        result.append(
            {
                "account_id": str(invoice.account_id),
                "account_number": getattr(account, "account_number", "") or "",
                "account_name": _account_name(account),
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number or "",
                "invoice_status": _enum(invoice.status),
                "currency": invoice.currency or "NGN",
                "balance_due": _money(invoice.balance_due),
                "billing_period_start": _iso(invoice.billing_period_start),
                "billing_period_end": _iso(invoice.billing_period_end),
                "issued_at": _iso(invoice.issued_at),
                "due_at": _iso(invoice.due_at),
                "splynx_invoice_id": invoice.splynx_invoice_id or "",
                "recommended_action": (
                    "review_with_cleanup_prepaid_phantom_ar;"
                    " prefer_void_for_unfunded_production_rows"
                ),
            }
        )
    return result


def find_stale_dunning_cases(db: Session) -> list[dict[str, Any]]:
    """Open/paused dunning cases whose account no longer has collectible overdue AR."""

    cases = (
        db.query(DunningCase, Subscriber)
        .join(Subscriber, Subscriber.id == DunningCase.account_id)
        .filter(
            DunningCase.status.in_([DunningCaseStatus.open, DunningCaseStatus.paused])
        )
        .order_by(DunningCase.started_at.asc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for case, account in cases:
        if has_overdue_balance(db, str(case.account_id)):
            continue
        recommended_action = (
            "manual_review_paused_case"
            if case.status == DunningCaseStatus.paused
            else "resolve_no_collectible_ar"
        )
        result.append(
            {
                "case_id": str(case.id),
                "account_id": str(case.account_id),
                "account_number": getattr(account, "account_number", "") or "",
                "account_name": _account_name(account),
                "case_status": _enum(case.status),
                "current_step": case.current_step or "",
                "started_at": _iso(case.started_at),
                "recommended_action": recommended_action,
            }
        )
    return result


def find_stale_overdue_locks(db: Session) -> list[dict[str, Any]]:
    """Active overdue locks whose account no longer has collectible overdue AR."""

    locks = (
        db.query(EnforcementLock, Subscriber, Subscription)
        .join(Subscriber, Subscriber.id == EnforcementLock.subscriber_id)
        .join(Subscription, Subscription.id == EnforcementLock.subscription_id)
        .filter(EnforcementLock.reason == EnforcementReason.overdue)
        .filter(EnforcementLock.is_active.is_(True))
        .order_by(EnforcementLock.created_at.asc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for lock, account, subscription in locks:
        if has_overdue_balance(db, str(lock.subscriber_id)):
            continue
        result.append(
            {
                "lock_id": str(lock.id),
                "account_id": str(lock.subscriber_id),
                "account_number": getattr(account, "account_number", "") or "",
                "account_name": _account_name(account),
                "subscription_id": str(lock.subscription_id),
                "subscription_status": _enum(subscription.status),
                "source": lock.source,
                "created_at": _iso(lock.created_at),
                "recommended_action": "restore_or_resolve_overdue_lock_no_collectible_ar",
            }
        )
    return result


def find_next_billing_anchor_drift(db: Session) -> list[dict[str, Any]]:
    """Prepaid subscriptions whose next billing anchor is behind paid coverage."""

    rows = (
        db.query(
            Subscription,
            Subscriber,
            func.max(Invoice.billing_period_end).label("paid_through"),
        )
        .select_from(Subscription)
        .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Invoice.is_active.is_(True))
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.status == InvoiceStatus.paid)
        .filter(Invoice.balance_due <= Decimal("0.00"))
        .filter(Invoice.billing_period_end.isnot(None))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(Subscription.status.in_(_RELEVANT_SUBSCRIPTION_STATUSES))
        .group_by(Subscription.id, Subscriber.id)
        .all()
    )
    result: list[dict[str, Any]] = []
    for subscription, account, paid_through in rows:
        if paid_through is None:
            continue
        next_billing_at = subscription.next_billing_at
        paid_through_utc = _as_utc(paid_through)
        current_next_billing_at = _as_utc(next_billing_at)
        if paid_through_utc is None:
            continue
        if (
            current_next_billing_at is not None
            and current_next_billing_at >= paid_through_utc
        ):
            continue
        result.append(
            {
                "account_id": str(subscription.subscriber_id),
                "account_number": getattr(account, "account_number", "") or "",
                "account_name": _account_name(account),
                "subscription_id": str(subscription.id),
                "subscription_status": _enum(subscription.status),
                "current_next_billing_at": _iso(next_billing_at),
                "paid_through": _iso(paid_through),
                "recommended_action": "advance_next_billing_at_to_paid_through",
            }
        )
    return result


def build_billing_cleanup_report(db: Session) -> BillingCleanupReport:
    """Build the read-only billing cleanup report."""

    return BillingCleanupReport(
        prepaid_collectible_ar=find_prepaid_collectible_ar(db),
        prepaid_overlaps=[
            asdict(candidate) for candidate in find_prepaid_overlap_candidates(db)
        ],
        stale_dunning_cases=find_stale_dunning_cases(db),
        stale_overdue_locks=find_stale_overdue_locks(db),
        billing_mode_drift=find_billing_mode_inconsistencies(db),
        next_billing_anchor_drift=find_next_billing_anchor_drift(db),
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_billing_cleanup_report(
    report: BillingCleanupReport, output_dir: str | Path
) -> dict[str, str]:
    """Write one CSV per cleanup bucket plus a JSON summary manifest."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for name, rows in {
        "prepaid_collectible_ar": report.prepaid_collectible_ar,
        "prepaid_overlaps": report.prepaid_overlaps,
        "stale_dunning_cases": report.stale_dunning_cases,
        "stale_overdue_locks": report.stale_overdue_locks,
        "billing_mode_drift": report.billing_mode_drift,
        "next_billing_anchor_drift": report.next_billing_anchor_drift,
    }.items():
        path = output / f"{name}.csv"
        _write_csv(path, rows)
        files[name] = str(path)

    summary_path = output / "summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(report.summary(), handle, indent=2, sort_keys=True)
    files["summary"] = str(summary_path)
    return files
