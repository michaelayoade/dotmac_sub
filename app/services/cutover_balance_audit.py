"""Read-only cutover balance invariant audit."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.common import round_money

OPENING_MEMO = "Prepaid opening balance @ cutover"
CUTOVER_ACTIVITY_AT = datetime(2026, 6, 16, 9, 8, tzinfo=UTC)
PAYMENT_ACTIVITY_AT = datetime(2026, 6, 16, tzinfo=UTC)
TOLERANCE = Decimal("0.01")


def _money(value: object) -> Decimal:
    return round_money(Decimal(str(value or 0)))


def _direction(drift: Decimal) -> str:
    if abs(drift) <= TOLERANCE:
        return "balanced"
    return "overcredited" if drift > 0 else "understated"


def _rows(db: Session):
    return db.execute(
        text(
            """
            WITH seeded AS (
                SELECT DISTINCT account_id
                FROM ledger_entries
                WHERE memo = :opening_memo
            ),
            ledger_net AS (
                SELECT le.account_id,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.currency = 'NGN'
                GROUP BY le.account_id
            ),
            open_ar AS (
                SELECT i.account_id, COALESCE(SUM(i.balance_due), 0) AS due
                FROM invoices i
                JOIN seeded seeded ON seeded.account_id = i.account_id
                WHERE i.is_active
                  AND i.balance_due > 0
                  AND i.status IN ('issued', 'partially_paid', 'overdue')
                  AND i.currency = 'NGN'
                GROUP BY i.account_id
            ),
            post_payments AS (
                SELECT p.account_id, COALESCE(SUM(p.amount), 0) AS amount
                FROM payments p
                JOIN seeded seeded ON seeded.account_id = p.account_id
                WHERE p.is_active
                  AND p.status = 'succeeded'
                  AND p.created_at >= :payment_at
                GROUP BY p.account_id
            ),
            post_invoices AS (
                SELECT i.account_id, COALESCE(SUM(i.total), 0) AS amount
                FROM invoices i
                JOIN seeded seeded ON seeded.account_id = i.account_id
                WHERE i.is_active
                  AND i.status IN ('issued', 'partially_paid', 'overdue', 'paid')
                  AND COALESCE(i.is_proforma, false) IS false
                  AND i.created_at >= :activity_at
                GROUP BY i.account_id
            ),
            seed_sums AS (
                SELECT le.account_id,
                       COALESCE(SUM(CASE
                         WHEN le.is_active AND le.entry_type = 'credit' THEN le.amount
                         WHEN le.is_active AND le.entry_type = 'debit' THEN -le.amount
                         ELSE 0 END), 0) AS active_seed_net,
                       COALESCE(SUM(CASE
                         WHEN NOT le.is_active AND le.entry_type = 'credit' THEN le.amount
                         WHEN NOT le.is_active AND le.entry_type = 'debit' THEN -le.amount
                         ELSE 0 END), 0) AS inactive_seed_net,
                       COALESCE(SUM(CASE
                         WHEN NOT le.is_active AND le.entry_type = 'debit' THEN le.amount
                         ELSE 0 END), 0) AS inactive_opening_debits
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.memo = :opening_memo
                GROUP BY le.account_id
            ),
            all_post_adjustments AS (
                SELECT le.account_id, COUNT(le.id) AS entry_count,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.source = 'adjustment'
                  AND le.memo <> :opening_memo
                  AND le.created_at >= :activity_at
                GROUP BY le.account_id
            ),
            target_adjustments AS (
                SELECT le.account_id, COUNT(le.id) AS entry_count,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.source = 'adjustment'
                  AND le.memo <> :opening_memo
                  AND le.memo NOT LIKE 'Reversal of phantom%'
                  AND le.memo NOT LIKE 'Reversal of prepaid opening%'
                  AND le.memo NOT LIKE 'Correction:%'
                  AND le.created_at >= :activity_at
                GROUP BY le.account_id
            )
            SELECT s.id AS account_id,
                   COALESCE(NULLIF(s.display_name, ''), NULLIF(s.company_name, ''),
                            concat_ws(' ', s.first_name, s.last_name)) AS subscriber_name,
                   s.status AS subscriber_status,
                   COALESCE(s.deposit, 0) AS deposit,
                   COALESCE(ln.net, 0) - COALESCE(oa.due, 0) AS current_available,
                   COALESCE(s.deposit, 0) + COALESCE(pp.amount, 0)
                     + COALESCE(ta.net, 0) - COALESCE(pi.amount, 0) AS target_available,
                   COALESCE(pp.amount, 0) AS post_cutover_payments,
                   COALESCE(pi.amount, 0) AS post_cutover_invoices,
                   COALESCE(ss.active_seed_net, 0) AS active_seed_net,
                   COALESCE(ss.inactive_seed_net, 0) AS inactive_seed_net,
                   COALESCE(ss.inactive_opening_debits, 0) AS inactive_opening_debits,
                   COALESCE(ta.entry_count, 0) AS target_adjustment_entry_count,
                   COALESCE(ta.net, 0) AS target_adjustment_net,
                   COALESCE(apa.entry_count, 0) AS post_adjustment_entry_count,
                   COALESCE(apa.net, 0) AS post_adjustment_net,
                   COALESCE(apa.entry_count, 0) - COALESCE(ta.entry_count, 0)
                     AS excluded_adjustment_entry_count,
                   COALESCE(apa.net, 0) - COALESCE(ta.net, 0)
                     AS excluded_adjustment_net
            FROM seeded seeded
            JOIN subscribers s ON s.id = seeded.account_id
            LEFT JOIN ledger_net ln ON ln.account_id = s.id
            LEFT JOIN open_ar oa ON oa.account_id = s.id
            LEFT JOIN post_payments pp ON pp.account_id = s.id
            LEFT JOIN post_invoices pi ON pi.account_id = s.id
            LEFT JOIN seed_sums ss ON ss.account_id = s.id
            LEFT JOIN all_post_adjustments apa ON apa.account_id = s.id
            LEFT JOIN target_adjustments ta ON ta.account_id = s.id
            """
        ),
        {
            "opening_memo": OPENING_MEMO,
            "activity_at": CUTOVER_ACTIVITY_AT,
            "payment_at": PAYMENT_ACTIVITY_AT,
        },
    ).mappings()


def audit_cutover_balance_invariant(db: Session, *, sample_limit: int = 25) -> dict[str, Any]:
    population = 0
    drift_rows: list[dict[str, Any]] = []
    overcredited_total = Decimal("0")
    understated_total = Decimal("0")
    post_adjustment_entry_count = 0
    post_adjustment_net = Decimal("0")
    target_adjustment_entry_count = 0
    target_adjustment_net = Decimal("0")
    excluded_adjustment_entry_count = 0
    excluded_adjustment_net = Decimal("0")
    inactive_seed_drift_count = 0
    post_adjustment_drift_count = 0

    for row in _rows(db):
        population += 1
        post_adjustment_entry_count += int(row["post_adjustment_entry_count"] or 0)
        post_adjustment_net += _money(row["post_adjustment_net"])
        target_adjustment_entry_count += int(row["target_adjustment_entry_count"] or 0)
        target_adjustment_net += _money(row["target_adjustment_net"])
        excluded_adjustment_entry_count += int(row["excluded_adjustment_entry_count"] or 0)
        excluded_adjustment_net += _money(row["excluded_adjustment_net"])

        current = _money(row["current_available"])
        target = _money(row["target_available"])
        drift = _money(current - target)
        if abs(drift) <= TOLERANCE:
            continue
        if drift > 0:
            overcredited_total += drift
        else:
            understated_total += abs(drift)
        if _money(row["inactive_opening_debits"]) != Decimal("0.00"):
            inactive_seed_drift_count += 1
        if _money(row["target_adjustment_net"]) != Decimal("0.00"):
            post_adjustment_drift_count += 1
        drift_rows.append(
            {
                "account_id": str(row["account_id"]),
                "subscriber_name": str(row["subscriber_name"] or ""),
                "subscriber_status": str(row["subscriber_status"] or ""),
                "current_available": str(current),
                "target_available": str(target),
                "drift": str(drift),
                "direction": _direction(drift),
            }
        )

    drift_rows.sort(key=lambda item: abs(Decimal(item["drift"])), reverse=True)
    overcredited = [row for row in drift_rows if Decimal(row["drift"]) > 0]
    understated = [row for row in drift_rows if Decimal(row["drift"]) < 0]
    return {
        "ok": not drift_rows,
        "population": population,
        "drift_count": len(drift_rows),
        "overcredited_count": len(overcredited),
        "overcredited_total": str(round_money(overcredited_total)),
        "understated_count": len(understated),
        "understated_total": str(round_money(understated_total)),
        "inactive_seed_drift_count": inactive_seed_drift_count,
        "post_adjustment_drift_count": post_adjustment_drift_count,
        "post_adjustment_entry_count": post_adjustment_entry_count,
        "post_adjustment_net": str(round_money(post_adjustment_net)),
        "target_adjustment_entry_count": target_adjustment_entry_count,
        "target_adjustment_net": str(round_money(target_adjustment_net)),
        "excluded_adjustment_entry_count": excluded_adjustment_entry_count,
        "excluded_adjustment_net": str(round_money(excluded_adjustment_net)),
        "sample_limit": sample_limit,
        "samples": drift_rows[:sample_limit],
    }
