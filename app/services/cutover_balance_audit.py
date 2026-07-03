"""Cutover balance invariant audit.

For migrated accounts with cutover opening-balance construction rows, the local
available balance should match the Splynx deposit truth rolled forward through
post-cutover local activity:

    current_available == subscribers.deposit
        + post-cutover succeeded payments
        + post-cutover active manual adjustments
        - post-cutover active non-void invoice totals

This is the durable guard for the June/July 2026 cutover remediation work. It is
read-only and intentionally set-based so it can run as a scheduled audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.common import round_money

CUTOVER_AT = datetime(2026, 6, 16, tzinfo=UTC)
OPENING_MEMO = "Prepaid opening balance @ cutover"
TOLERANCE = Decimal("0.01")
DEFAULT_SAMPLE_LIMIT = 25


@dataclass(frozen=True)
class CutoverBalanceDrift:
    account_id: str
    subscriber_name: str
    subscriber_status: str
    deposit: Decimal
    current_available: Decimal
    target_available: Decimal
    drift: Decimal
    direction: str
    post_cutover_payments: Decimal
    post_cutover_invoices: Decimal
    active_seed_net: Decimal
    inactive_seed_net: Decimal
    active_opening_debits: Decimal
    inactive_opening_debits: Decimal
    post_adjustment_net: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "subscriber_name": self.subscriber_name,
            "subscriber_status": self.subscriber_status,
            "deposit": str(self.deposit),
            "current_available": str(self.current_available),
            "target_available": str(self.target_available),
            "drift": str(self.drift),
            "direction": self.direction,
            "post_cutover_payments": str(self.post_cutover_payments),
            "post_cutover_invoices": str(self.post_cutover_invoices),
            "active_seed_net": str(self.active_seed_net),
            "inactive_seed_net": str(self.inactive_seed_net),
            "active_opening_debits": str(self.active_opening_debits),
            "inactive_opening_debits": str(self.inactive_opening_debits),
            "post_adjustment_net": str(self.post_adjustment_net),
        }


def _money(value) -> Decimal:
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
                SELECT
                    le.account_id,
                    COALESCE(SUM(
                        CASE
                            WHEN le.entry_type = 'credit' THEN le.amount
                            ELSE -le.amount
                        END
                    ), 0) AS net
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
                  AND p.created_at >= :cutover_at
                GROUP BY p.account_id
            ),
            post_invoices AS (
                SELECT i.account_id, COALESCE(SUM(i.total), 0) AS amount
                FROM invoices i
                JOIN seeded seeded ON seeded.account_id = i.account_id
                WHERE i.is_active
                  AND i.status <> 'void'
                  AND COALESCE(i.is_proforma, false) IS false
                  AND i.created_at >= :cutover_at
                GROUP BY i.account_id
            ),
            seed_sums AS (
                SELECT
                    le.account_id,
                    COALESCE(SUM(
                        CASE
                            WHEN le.is_active AND le.entry_type = 'credit'
                                THEN le.amount
                            WHEN le.is_active AND le.entry_type = 'debit'
                                THEN -le.amount
                            ELSE 0
                        END
                    ), 0) AS active_seed_net,
                    COALESCE(SUM(
                        CASE
                            WHEN NOT le.is_active AND le.entry_type = 'credit'
                                THEN le.amount
                            WHEN NOT le.is_active AND le.entry_type = 'debit'
                                THEN -le.amount
                            ELSE 0
                        END
                    ), 0) AS inactive_seed_net,
                    COALESCE(SUM(
                        CASE
                            WHEN le.is_active AND le.entry_type = 'debit'
                                THEN le.amount
                            ELSE 0
                        END
                    ), 0) AS active_opening_debits,
                    COALESCE(SUM(
                        CASE
                            WHEN NOT le.is_active AND le.entry_type = 'debit'
                                THEN le.amount
                            ELSE 0
                        END
                    ), 0) AS inactive_opening_debits
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.memo = :opening_memo
                GROUP BY le.account_id
            ),
            post_adjustments AS (
                SELECT
                    le.account_id,
                    COALESCE(SUM(
                        CASE
                            WHEN le.entry_type = 'credit' THEN le.amount
                            ELSE -le.amount
                        END
                    ), 0) AS net
                FROM ledger_entries le
                JOIN seeded seeded ON seeded.account_id = le.account_id
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.source = 'adjustment'
                  AND le.memo <> :opening_memo
                  AND le.created_at >= :cutover_at
                GROUP BY le.account_id
            )
            SELECT
                s.id AS account_id,
                COALESCE(
                    NULLIF(s.display_name, ''),
                    NULLIF(s.company_name, ''),
                    concat_ws(' ', s.first_name, s.last_name)
                ) AS subscriber_name,
                s.status AS subscriber_status,
                COALESCE(s.deposit, 0) AS deposit,
                COALESCE(ln.net, 0) - COALESCE(oa.due, 0) AS current_available,
                COALESCE(s.deposit, 0)
                    + COALESCE(pp.amount, 0)
                    + COALESCE(pa.net, 0)
                    - COALESCE(pi.amount, 0) AS target_available,
                COALESCE(pp.amount, 0) AS post_cutover_payments,
                COALESCE(pi.amount, 0) AS post_cutover_invoices,
                COALESCE(ss.active_seed_net, 0) AS active_seed_net,
                COALESCE(ss.inactive_seed_net, 0) AS inactive_seed_net,
                COALESCE(ss.active_opening_debits, 0) AS active_opening_debits,
                COALESCE(ss.inactive_opening_debits, 0) AS inactive_opening_debits,
                COALESCE(pa.net, 0) AS post_adjustment_net
            FROM seeded seeded
            JOIN subscribers s ON s.id = seeded.account_id
            LEFT JOIN ledger_net ln ON ln.account_id = s.id
            LEFT JOIN open_ar oa ON oa.account_id = s.id
            LEFT JOIN post_payments pp ON pp.account_id = s.id
            LEFT JOIN post_invoices pi ON pi.account_id = s.id
            LEFT JOIN seed_sums ss ON ss.account_id = s.id
            LEFT JOIN post_adjustments pa ON pa.account_id = s.id
            """
        ),
        {"opening_memo": OPENING_MEMO, "cutover_at": CUTOVER_AT},
    ).mappings()


def audit_cutover_balance_invariant(
    db: Session, *, sample_limit: int = DEFAULT_SAMPLE_LIMIT
) -> dict[str, Any]:
    population = 0
    drift_rows: list[CutoverBalanceDrift] = []
    overcredited_total = Decimal("0")
    understated_total = Decimal("0")
    post_adjustment_drift = 0
    inactive_seed_drift = 0

    for row in _rows(db):
        population += 1
        current = _money(row["current_available"])
        target = _money(row["target_available"])
        drift = _money(current - target)
        if abs(drift) <= TOLERANCE:
            continue
        direction = _direction(drift)
        if drift > 0:
            overcredited_total += drift
        else:
            understated_total += abs(drift)
        if _money(row["post_adjustment_net"]) != Decimal("0.00"):
            post_adjustment_drift += 1
        if _money(row["inactive_opening_debits"]) != Decimal("0.00"):
            inactive_seed_drift += 1
        drift_rows.append(
            CutoverBalanceDrift(
                account_id=str(row["account_id"]),
                subscriber_name=str(row["subscriber_name"] or ""),
                subscriber_status=str(row["subscriber_status"] or ""),
                deposit=_money(row["deposit"]),
                current_available=current,
                target_available=target,
                drift=drift,
                direction=direction,
                post_cutover_payments=_money(row["post_cutover_payments"]),
                post_cutover_invoices=_money(row["post_cutover_invoices"]),
                active_seed_net=_money(row["active_seed_net"]),
                inactive_seed_net=_money(row["inactive_seed_net"]),
                active_opening_debits=_money(row["active_opening_debits"]),
                inactive_opening_debits=_money(row["inactive_opening_debits"]),
                post_adjustment_net=_money(row["post_adjustment_net"]),
            )
        )

    drift_rows.sort(key=lambda item: abs(item.drift), reverse=True)
    overcredited = [row for row in drift_rows if row.drift > 0]
    understated = [row for row in drift_rows if row.drift < 0]
    return {
        "ok": not drift_rows,
        "population": population,
        "drift_count": len(drift_rows),
        "overcredited_count": len(overcredited),
        "overcredited_total": str(round_money(overcredited_total)),
        "understated_count": len(understated),
        "understated_total": str(round_money(understated_total)),
        "post_adjustment_drift_count": post_adjustment_drift,
        "inactive_seed_drift_count": inactive_seed_drift,
        "sample_limit": sample_limit,
        "samples": [row.as_dict() for row in drift_rows[:sample_limit]],
    }
