"""Consolidated dry-run audit for billing cleanup after mode/anchor drift."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    AddOn,
    AddOnPrice,
    BillingMode,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber
from app.services.billing_mode_audit import find_billing_mode_inconsistencies
from app.services.billing_prepaid_overlap_repair import find_prepaid_overlap_candidates
from app.services.collections._core import has_overdue_balance
from app.services.ip_consistency_audit import _external_ip_state
from app.services.radius import _external_password_row

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
_TERMINAL_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.disabled,
)
_VOID = InvoiceStatus.void.value


@dataclass(frozen=True)
class BillingCleanupReport:
    prepaid_collectible_ar: list[dict[str, Any]]
    prepaid_overlaps: list[dict[str, Any]]
    billing_disabled_service_lines: list[dict[str, Any]]
    billing_duplicate_subscription_period_lines: list[dict[str, Any]]
    billing_addon_without_billable_parent: list[dict[str, Any]]
    active_subscription_missing_radius: list[dict[str, Any]]
    stale_dunning_cases: list[dict[str, Any]]
    stale_overdue_locks: list[dict[str, Any]]
    billing_mode_drift: list[dict[str, Any]]
    next_billing_anchor_drift: list[dict[str, Any]]

    def summary(self) -> dict[str, int]:
        return {
            "prepaid_collectible_ar": len(self.prepaid_collectible_ar),
            "prepaid_overlaps": len(self.prepaid_overlaps),
            "billing_disabled_service_lines": len(self.billing_disabled_service_lines),
            "billing_duplicate_subscription_period_lines": len(
                self.billing_duplicate_subscription_period_lines
            ),
            "billing_addon_without_billable_parent": len(
                self.billing_addon_without_billable_parent
            ),
            "active_subscription_missing_radius": len(
                self.active_subscription_missing_radius
            ),
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


def _classify_billing_line(
    finding_type: str,
    invoice_status: str,
    period_start: datetime | None,
    ended_at: datetime | None,
) -> tuple[str, str]:
    """Classify a billing-line violation for finance/remediation review."""

    if finding_type == "disabled_service":
        if period_start is None or ended_at is None:
            return "manual_finance_review", "ambiguous: missing period or end date"
        if invoice_status == _VOID:
            return "valid_historical_charge", "invoice already void"
        return (
            "credit_or_void_required",
            "line bills a period starting after the service ended",
        )
    if period_start is None:
        return "manual_finance_review", "ambiguous: missing billing period"
    if invoice_status == _VOID:
        return "valid_historical_charge", "duplicate line on already-void invoice"
    return (
        "duplicate_review",
        "same subscription, period, and description billed more than once",
    )


def _billing_line_row(
    row: Any,
    finding_type: str,
    *,
    duplicate_group_key: str = "",
    duplicate_group_count: int | str = "",
) -> dict[str, Any]:
    ended_at = row.canceled_at or row.end_at
    invoice_status = _enum(row.invoice_status)
    disposition, reason = _classify_billing_line(
        finding_type, invoice_status, row.billing_period_start, ended_at
    )
    return {
        "finding_type": finding_type,
        "invoice_id": str(row.invoice_id),
        "invoice_number": row.invoice_number or "",
        "invoice_status": invoice_status,
        "invoice_line_id": str(row.line_id),
        "subscription_id": str(row.subscription_id),
        "subscriber_id": str(row.subscriber_id),
        "splynx_customer_id": row.splynx_customer_id or "",
        "customer_name": (
            row.display_name or f"{row.first_name or ''} {row.last_name or ''}".strip()
        ),
        "service_status": _enum(row.service_status),
        "subscriber_status": _enum(row.subscriber_status),
        "billing_period_start": _iso(row.billing_period_start),
        "billing_period_end": _iso(row.billing_period_end),
        "line_description": row.description or "",
        "line_amount": _money(row.amount),
        "invoice_total": _money(row.total),
        "invoice_balance_due": _money(row.balance_due),
        "created_at": _iso(row.created_at),
        "line_created_at": _iso(row.line_created_at),
        "canceled_or_end_at": _iso(ended_at),
        "duplicate_group_key": duplicate_group_key,
        "duplicate_group_count": duplicate_group_count,
        "proposed_disposition": disposition,
        "recommended_action": disposition,
        "reason": reason,
    }


def _billing_line_select():
    return (
        select(
            InvoiceLine.id.label("line_id"),
            InvoiceLine.subscription_id,
            InvoiceLine.description,
            InvoiceLine.amount,
            InvoiceLine.created_at.label("line_created_at"),
            Invoice.id.label("invoice_id"),
            Invoice.invoice_number,
            Invoice.status.label("invoice_status"),
            Invoice.billing_period_start,
            Invoice.billing_period_end,
            Invoice.total,
            Invoice.balance_due,
            Invoice.created_at,
            Subscription.status.label("service_status"),
            Subscription.canceled_at,
            Subscription.end_at,
            Subscription.subscriber_id,
            Subscriber.splynx_customer_id,
            Subscriber.display_name,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.status.label("subscriber_status"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .where(InvoiceLine.is_active.is_(True))
        .where(Invoice.is_active.is_(True))
    )


def find_billing_disabled_service_lines(db: Session) -> list[dict[str, Any]]:
    """Line-level worklist for terminal services billed after their end date."""

    ended_at = func.coalesce(Subscription.canceled_at, Subscription.end_at)
    rows = db.execute(
        _billing_line_select()
        .where(Subscription.status.in_(_TERMINAL_SUBSCRIPTION_STATUSES))
        .where(ended_at.isnot(None))
        .where(Invoice.billing_period_start.isnot(None))
        .where(Invoice.billing_period_start > ended_at)
        .order_by(Subscriber.display_name.asc().nulls_last(), Invoice.created_at.asc())
    ).all()
    return [_billing_line_row(row, "disabled_service") for row in rows]


def find_billing_duplicate_subscription_period_lines(
    db: Session,
) -> list[dict[str, Any]]:
    """Line-level worklist for duplicate subscription-period invoice lines."""

    groups = db.execute(
        select(
            InvoiceLine.subscription_id,
            Invoice.billing_period_start,
            Invoice.billing_period_end,
            InvoiceLine.description,
            func.count().label("n"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(InvoiceLine.is_active.is_(True))
        .where(Invoice.is_active.is_(True))
        .where(InvoiceLine.subscription_id.isnot(None))
        .where(Invoice.billing_period_start.isnot(None))
        .group_by(
            InvoiceLine.subscription_id,
            Invoice.billing_period_start,
            Invoice.billing_period_end,
            InvoiceLine.description,
        )
        .having(func.count() > 1)
    ).all()
    if not groups:
        return []
    group_counts = {
        (str(g[0]), _iso(g[1]), _iso(g[2]), g[3] or ""): int(g[4]) for g in groups
    }
    subscription_ids = [g[0] for g in groups]
    rows = db.execute(
        _billing_line_select()
        .where(InvoiceLine.subscription_id.in_(subscription_ids))
        .order_by(
            InvoiceLine.subscription_id.asc(),
            Invoice.billing_period_start.asc(),
            InvoiceLine.description.asc(),
            InvoiceLine.created_at.asc(),
            InvoiceLine.id.asc(),
        )
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.subscription_id),
            _iso(row.billing_period_start),
            _iso(row.billing_period_end),
            row.description or "",
        )
        count = group_counts.get(key)
        if count is None:
            continue
        result.append(
            _billing_line_row(
                row,
                "duplicate_period",
                duplicate_group_key="|".join(key),
                duplicate_group_count=count,
            )
        )
    return result


def find_billing_addon_without_billable_parent(db: Session) -> list[dict[str, Any]]:
    """Recurring add-ons that outlive a terminal parent subscription."""

    now = datetime.now(UTC)
    recurring_addons = (
        select(AddOnPrice.add_on_id)
        .where(AddOnPrice.price_type == PriceType.recurring)
        .where(AddOnPrice.is_active.is_(True))
        .distinct()
        .subquery()
    )
    rows = (
        db.query(SubscriptionAddOn, Subscription, Subscriber, AddOn)
        .join(Subscription, Subscription.id == SubscriptionAddOn.subscription_id)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(Subscription.status.in_(_TERMINAL_SUBSCRIPTION_STATUSES))
        .filter(
            or_(
                SubscriptionAddOn.end_at.is_(None),
                SubscriptionAddOn.end_at > now,
            )
        )
        .filter(SubscriptionAddOn.add_on_id.in_(select(recurring_addons)))
        .order_by(Subscriber.display_name.asc().nulls_last())
        .all()
    )
    result: list[dict[str, Any]] = []
    for sub_addon, subscription, account, addon in rows:
        ended_at = subscription.canceled_at or subscription.end_at
        result.append(
            {
                "subscription_add_on_id": str(sub_addon.id),
                "subscription_id": str(subscription.id),
                "subscriber_id": str(subscription.subscriber_id),
                "account_number": getattr(account, "account_number", "") or "",
                "account_name": _account_name(account),
                "subscription_status": _enum(subscription.status),
                "add_on_id": str(addon.id),
                "add_on_name": addon.name,
                "current_end_at": _iso(sub_addon.end_at),
                "parent_ended_at": _iso(ended_at),
                "recommended_action": "end_addon_at_parent_end",
            }
        )
    return result


def find_active_subscription_missing_radius(db: Session) -> list[dict[str, Any]]:
    """Active subscriptions whose login is absent from external RADIUS."""

    subscriptions = (
        db.query(Subscription, Subscriber)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscription.login.isnot(None))
        .order_by(Subscription.login.asc())
        .all()
    )
    logins = sorted(
        {
            subscription.login.strip()
            for subscription, _ in subscriptions
            if subscription.login
        }
    )
    if not logins:
        return []
    _framed, provisioned, _errors = _external_ip_state(db, logins)
    missing = {login for login in logins if login not in provisioned}
    if not missing:
        return []
    credentials = {
        (str(credential.subscriber_id), credential.username): credential
        for credential in db.scalars(
            select(AccessCredential).where(AccessCredential.is_active.is_(True))
        ).all()
    }
    result: list[dict[str, Any]] = []
    for subscription, account in subscriptions:
        login = (subscription.login or "").strip()
        if login not in missing:
            continue
        credential = credentials.get((str(subscription.subscriber_id), login))
        usable = (
            credential is not None
            and _external_password_row(
                credential,
                default_attribute="Cleartext-Password",
                default_op=":=",
            )
            is not None
        )
        result.append(
            {
                "subscription_id": str(subscription.id),
                "subscriber_id": str(subscription.subscriber_id),
                "account_number": getattr(account, "account_number", "") or "",
                "account_name": _account_name(account),
                "login": login,
                "credential_id": str(credential.id) if credential else "",
                "credential_usable": str(bool(usable)).lower(),
                "recommended_action": (
                    "sync_radius_connectivity"
                    if usable
                    else "reset_pppoe_password_then_sync"
                ),
            }
        )
    return result


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
        billing_disabled_service_lines=find_billing_disabled_service_lines(db),
        billing_duplicate_subscription_period_lines=(
            find_billing_duplicate_subscription_period_lines(db)
        ),
        billing_addon_without_billable_parent=(
            find_billing_addon_without_billable_parent(db)
        ),
        active_subscription_missing_radius=find_active_subscription_missing_radius(db),
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
        "billing_disabled_service_lines": report.billing_disabled_service_lines,
        "billing_duplicate_subscription_period_lines": (
            report.billing_duplicate_subscription_period_lines
        ),
        "billing_addon_without_billable_parent": (
            report.billing_addon_without_billable_parent
        ),
        "active_subscription_missing_radius": report.active_subscription_missing_radius,
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
