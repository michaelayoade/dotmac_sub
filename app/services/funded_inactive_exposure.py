"""Read-only funded inactive account exposure audit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.common import round_money

INACTIVE_STATUSES = ("blocked", "disabled", "suspended")
TOLERANCE = Decimal("0.01")
MATERIAL_AMOUNT = Decimal("50000.00")


def _money(value: object) -> Decimal:
    return round_money(Decimal(str(value or 0)))


def _isoformat(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _rows(db: Session, *, min_amount: Decimal = TOLERANCE):
    return db.execute(
        text(
            """
            WITH ledger_net AS (
                SELECT le.account_id,
                       COALESCE(SUM(CASE WHEN le.entry_type = 'credit'
                                         THEN le.amount ELSE -le.amount END), 0) AS net
                FROM ledger_entries le
                WHERE le.is_active
                  AND le.invoice_id IS NULL
                  AND le.currency = 'NGN'
                GROUP BY le.account_id
            ),
            open_ar AS (
                SELECT i.account_id, COALESCE(SUM(i.balance_due), 0) AS due
                FROM invoices i
                WHERE i.is_active
                  AND i.balance_due > 0
                  AND i.status IN ('issued', 'partially_paid', 'overdue')
                  AND i.currency = 'NGN'
                GROUP BY i.account_id
            ),
            ticket_counts AS (
                SELECT st.subscriber_id, COUNT(*) AS ticket_count
                FROM support_tickets st
                WHERE st.is_active
                GROUP BY st.subscriber_id
            ),
            status_events AS (
                SELECT ssh.subscriber_id, COUNT(*) AS status_event_count,
                       MAX(ssh.created_at) AS latest_status_event_at
                FROM subscriber_status_history ssh
                GROUP BY ssh.subscriber_id
            ),
            exposure AS (
                SELECT s.id AS account_id,
                       COALESCE(NULLIF(s.display_name, ''), NULLIF(s.company_name, ''),
                                concat_ws(' ', s.first_name, s.last_name))
                         AS subscriber_name,
                       s.status AS subscriber_status,
                       s.splynx_customer_id,
                       COALESCE(s.deposit, 0) AS deposit,
                       COALESCE(ln.net, 0) - COALESCE(oa.due, 0)
                         AS current_available,
                       COALESCE(oa.due, 0) AS open_ar,
                       COALESCE(tc.ticket_count, 0) AS ticket_count,
                       COALESCE(se.status_event_count, 0) AS status_event_count,
                       se.latest_status_event_at,
                       s.updated_at
                FROM subscribers s
                LEFT JOIN ledger_net ln ON ln.account_id = s.id
                LEFT JOIN open_ar oa ON oa.account_id = s.id
                LEFT JOIN ticket_counts tc ON tc.subscriber_id = s.id
                LEFT JOIN status_events se ON se.subscriber_id = s.id
                WHERE s.is_active
                  AND s.status IN ('blocked', 'disabled', 'suspended')
            )
            SELECT *
            FROM exposure
            WHERE current_available > :min_amount
            ORDER BY current_available DESC, subscriber_name ASC
            """
        ),
        {"min_amount": min_amount},
    ).mappings()


def _empty_status_summary() -> dict[str, dict[str, Any]]:
    return {
        status: {"count": 0, "total": "0.00", "material_count": 0}
        for status in INACTIVE_STATUSES
    }


def funded_inactive_exposure(
    db: Session,
    *,
    sample_limit: int = 25,
    min_amount: Decimal = TOLERANCE,
    material_amount: Decimal = MATERIAL_AMOUNT,
) -> dict[str, Any]:
    """Summarize inactive accounts still carrying customer-positive balance."""

    totals: dict[str, Decimal] = {
        status: Decimal("0.00") for status in INACTIVE_STATUSES
    }
    counts: dict[str, int] = dict.fromkeys(INACTIVE_STATUSES, 0)
    material_counts: dict[str, int] = dict.fromkeys(INACTIVE_STATUSES, 0)
    rows: list[dict[str, Any]] = []

    for row in _rows(db, min_amount=min_amount):
        status = str(row["subscriber_status"] or "")
        if status not in counts:
            continue
        current_available = _money(row["current_available"])
        deposit = _money(row["deposit"])
        open_ar = _money(row["open_ar"])
        counts[status] += 1
        totals[status] += current_available
        if current_available >= material_amount:
            material_counts[status] += 1
        rows.append(
            {
                "account_id": str(row["account_id"]),
                "subscriber_name": str(row["subscriber_name"] or ""),
                "subscriber_status": status,
                "splynx_customer_id": (
                    None
                    if row["splynx_customer_id"] is None
                    else str(row["splynx_customer_id"])
                ),
                "current_available": str(current_available),
                "deposit": str(deposit),
                "open_ar": str(open_ar),
                "ticket_count": int(row["ticket_count"] or 0),
                "status_event_count": int(row["status_event_count"] or 0),
                "latest_status_event_at": _isoformat(row["latest_status_event_at"]),
                "updated_at": _isoformat(row["updated_at"]),
            }
        )

    rows.sort(key=lambda item: Decimal(item["current_available"]), reverse=True)
    status_summary = _empty_status_summary()
    for status in INACTIVE_STATUSES:
        status_summary[status] = {
            "count": counts[status],
            "total": str(round_money(totals[status])),
            "material_count": material_counts[status],
        }

    inactive_positive_total = sum(totals.values(), Decimal("0.00"))
    disabled_count = counts["disabled"]
    suspended_count = counts["suspended"]
    return {
        "ok": disabled_count == 0 and suspended_count == 0,
        "inactive_positive_count": sum(counts.values()),
        "inactive_positive_total": str(round_money(inactive_positive_total)),
        "disabled_count": disabled_count,
        "disabled_total": str(round_money(totals["disabled"])),
        "blocked_count": counts["blocked"],
        "blocked_total": str(round_money(totals["blocked"])),
        "suspended_count": suspended_count,
        "suspended_total": str(round_money(totals["suspended"])),
        "material_amount": str(material_amount),
        "material_count": sum(material_counts.values()),
        "by_status": status_summary,
        "sample_limit": sample_limit,
        "samples": rows[:sample_limit],
    }
