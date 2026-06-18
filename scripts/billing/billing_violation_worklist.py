#!/usr/bin/env python
"""Finance decision packet for billing-integrity violations (READ-ONLY).

Builds a per-line worklist for the two launch-blocking billing findings:
  - disabled_service : an active line billing a TERMINAL subscription for a
    period that STARTS after the service ended (canceled_at/end_at).
  - duplicate_period : >1 active line with the same (subscription, billing
    period, description).

Writes ONE annotated source CSV plus per-disposition split worklists. WRITES
NOTHING to the DB and proposes NO money mutation — it produces a finance
decision packet only. See docs/POST_CUTOVER_BILLING_VIOLATIONS.md.

Usage (in the app container):
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/billing_violation_worklist.py
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from decimal import Decimal

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceLine
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber

_TERMINAL = (
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.disabled,
)
_VOID = "void"

ANNOTATED = "/app/billing_violations_annotated.csv"
SPLITS = {
    "credit_or_void_required": "/app/billing_violations_credit_or_void.csv",
    "duplicate_review": "/app/billing_violations_duplicate_review.csv",
    "valid_historical_charge": "/app/billing_violations_valid_historical.csv",
    "manual_finance_review": "/app/billing_violations_manual_review.csv",
}

COLUMNS = [
    "finding_type",
    "invoice_id",
    "invoice_number",
    "invoice_status",
    "invoice_line_id",
    "subscription_id",
    "subscriber_id",
    "splynx_customer_id",
    "customer_name",
    "service_status",
    "subscriber_status",
    "billing_period_start",
    "billing_period_end",
    "line_description",
    "line_amount",
    "invoice_total",
    "invoice_balance_due",
    "created_at",
    "canceled_or_end_at",
    "duplicate_group_key",
    "duplicate_group_count",
    "proposed_disposition",
    "reason",
]


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


def _enum(v) -> str:
    return getattr(v, "value", str(v)) if v is not None else ""


def _base_select():
    return (
        select(
            InvoiceLine.id.label("line_id"),
            InvoiceLine.subscription_id,
            InvoiceLine.description,
            InvoiceLine.amount,
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


def _row(r, finding_type, group_key="", group_count="") -> dict:
    invoice_status = _enum(r.invoice_status)
    ended = r.canceled_at or r.end_at
    period_start = r.billing_period_start
    disposition, reason = _classify(finding_type, invoice_status, period_start, ended)
    return {
        "finding_type": finding_type,
        "invoice_id": str(r.invoice_id),
        "invoice_number": r.invoice_number or "",
        "invoice_status": invoice_status,
        "invoice_line_id": str(r.line_id),
        "subscription_id": str(r.subscription_id),
        "subscriber_id": str(r.subscriber_id),
        "splynx_customer_id": r.splynx_customer_id or "",
        "customer_name": (
            r.display_name or f"{r.first_name or ''} {r.last_name or ''}".strip()
        ),
        "service_status": _enum(r.service_status),
        "subscriber_status": _enum(r.subscriber_status),
        "billing_period_start": _iso(r.billing_period_start),
        "billing_period_end": _iso(r.billing_period_end),
        "line_description": r.description or "",
        "line_amount": str(Decimal(str(r.amount or 0))),
        "invoice_total": str(Decimal(str(r.total or 0))),
        "invoice_balance_due": str(Decimal(str(r.balance_due or 0))),
        "created_at": _iso(r.created_at),
        "canceled_or_end_at": _iso(ended),
        "duplicate_group_key": group_key,
        "duplicate_group_count": group_count,
        "proposed_disposition": disposition,
        "reason": reason,
    }


def _classify(finding_type, invoice_status, period_start, ended):
    """Conservative defaults; ambiguous → manual_finance_review."""
    if finding_type == "disabled_service":
        if period_start is None or ended is None:
            return (
                "manual_finance_review",
                "ambiguous: missing period_start or end date",
            )
        if invoice_status == _VOID:
            return (
                "valid_historical_charge",
                "invoice already void — no money to recover",
            )
        return (
            "credit_or_void_required",
            "line bills a period starting after the service ended "
            "(canceled/expired/disabled)",
        )
    # duplicate_period
    if period_start is None:
        return "manual_finance_review", "ambiguous: missing billing period"
    if invoice_status == _VOID:
        return "valid_historical_charge", "duplicate line on an already-void invoice"
    return (
        "duplicate_review",
        "same subscription + billing period + description billed more than once",
    )


def main() -> int:
    db = SessionLocal()
    try:
        rows: list[dict] = []

        # --- disabled_service ------------------------------------------------
        ended_at = func.coalesce(Subscription.canceled_at, Subscription.end_at)
        disabled = db.execute(
            _base_select()
            .where(Subscription.status.in_(_TERMINAL))
            .where(ended_at.isnot(None))
            .where(Invoice.billing_period_start.isnot(None))
            .where(Invoice.billing_period_start > ended_at)
        ).all()
        for r in disabled:
            rows.append(_row(r, "disabled_service"))

        # --- duplicate_period ------------------------------------------------
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
        group_count = {
            (str(g[0]), _iso(g[1]), _iso(g[2]), g[3] or ""): g[4] for g in groups
        }
        group_sub_ids = {str(g[0]) for g in groups}
        if group_sub_ids:
            candidates = db.execute(
                _base_select().where(
                    InvoiceLine.subscription_id.in_([g[0] for g in groups])
                )
            ).all()
            for r in candidates:
                key = (
                    str(r.subscription_id),
                    _iso(r.billing_period_start),
                    _iso(r.billing_period_end),
                    r.description or "",
                )
                if key in group_count:
                    rows.append(
                        _row(
                            r,
                            "duplicate_period",
                            group_key="|".join(key),
                            group_count=group_count[key],
                        )
                    )

        # --- write annotated + splits ---------------------------------------
        def _write(path, subset):
            with open(path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=COLUMNS)
                w.writeheader()
                w.writerows(subset)

        _write(ANNOTATED, rows)
        for disp, path in SPLITS.items():
            _write(path, [r for r in rows if r["proposed_disposition"] == disp])

        # --- summary ---------------------------------------------------------
        print("=== billing violation worklist (READ-ONLY) ===")
        print(f"annotated source : {ANNOTATED}  ({len(rows)} lines)")
        by_finding = Counter(r["finding_type"] for r in rows)
        print("\n-- by finding_type (lines) --")
        for k, n in by_finding.most_common():
            print(f"  {k:20s} {n:>5}")
        print(
            f"  disabled subs: "
            f"{len({r['subscription_id'] for r in rows if r['finding_type'] == 'disabled_service'})}"
            f"   duplicate groups: {len(group_count)}"
        )
        print("\n-- by proposed_disposition (lines) --")
        by_disp = Counter(r["proposed_disposition"] for r in rows)
        for k, n in by_disp.most_common():
            print(f"  {k:26s} {n:>5}  -> {SPLITS.get(k, '')}")
        amt = sum(Decimal(r["line_amount"]) for r in rows)
        credit = sum(
            Decimal(r["line_amount"])
            for r in rows
            if r["proposed_disposition"] == "credit_or_void_required"
        )
        print(f"\ntotal line amount across findings : NGN {amt:,.2f}")
        print(f"  of which credit_or_void_required: NGN {credit:,.2f}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
