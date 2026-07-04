"""Read-only funded inactive account exposure audit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.common import round_money

INACTIVE_STATUSES = ("blocked", "disabled", "suspended", "canceled")
REFUND_REVIEW_STATUSES = ("disabled", "suspended", "canceled")
TOLERANCE = Decimal("0.01")
MATERIAL_AMOUNT = Decimal("50000.00")
SIBLING_SAMPLE_LIMIT = 5


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
                       s.is_active AS subscriber_is_active,
                       s.splynx_customer_id,
                       s.email,
                       s.phone,
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
                WHERE s.status IN ('blocked', 'disabled', 'suspended', 'canceled')
            ),
            funded_exposure AS (
                SELECT *
                FROM exposure
                WHERE current_available > :min_amount
            )
            SELECT fe.account_id,
                   fe.subscriber_name,
                   fe.subscriber_status,
                   fe.subscriber_is_active,
                   fe.splynx_customer_id,
                   fe.deposit,
                   fe.current_available,
                   fe.open_ar,
                   fe.ticket_count,
                   fe.status_event_count,
                   fe.latest_status_event_at,
                   COALESCE(sc.active_sibling_count, 0) AS active_sibling_count,
                   sc.active_sibling_account_ids,
                   sc.active_sibling_names,
                   fe.updated_at
            FROM funded_exposure fe
            LEFT JOIN LATERAL (
                WITH sibling_matches AS (
                    SELECT sib.id,
                           COALESCE(NULLIF(sib.display_name, ''),
                                    NULLIF(sib.company_name, ''),
                                    concat_ws(' ', sib.first_name, sib.last_name))
                             AS sibling_name,
                           sib.updated_at
                    FROM subscribers sib
                    WHERE sib.id <> fe.account_id
                      AND sib.is_active
                      AND sib.status IN ('active', 'delinquent')
                      AND (
                        (
                          fe.splynx_customer_id IS NOT NULL
                          AND sib.splynx_customer_id = fe.splynx_customer_id
                        )
                        OR (
                          NULLIF(lower(trim(fe.email)), '') IS NOT NULL
                          AND lower(trim(fe.email)) = lower(trim(sib.email))
                        )
                        OR (
                          NULLIF(
                            regexp_replace(COALESCE(fe.phone, ''), '\\D', '', 'g'), ''
                          ) IS NOT NULL
                          AND regexp_replace(COALESCE(fe.phone, ''), '\\D', '', 'g')
                            = regexp_replace(COALESCE(sib.phone, ''), '\\D', '', 'g')
                        )
                      )
                ),
                sibling_sample AS (
                    SELECT *
                    FROM sibling_matches
                    ORDER BY updated_at DESC
                    LIMIT :sibling_sample_limit
                )
                SELECT (SELECT COUNT(*) FROM sibling_matches) AS active_sibling_count,
                       (
                         SELECT STRING_AGG(id::text, ',' ORDER BY updated_at DESC)
                         FROM sibling_sample
                       ) AS active_sibling_account_ids,
                       (
                         SELECT STRING_AGG(sibling_name, ' | ' ORDER BY updated_at DESC)
                         FROM sibling_sample
                       ) AS active_sibling_names
            ) sc ON TRUE
            ORDER BY fe.current_available DESC, fe.subscriber_name ASC
            """
        ),
        {"min_amount": min_amount, "sibling_sample_limit": SIBLING_SAMPLE_LIMIT},
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
    soft_deleted_count = 0
    soft_deleted_total = Decimal("0.00")
    sibling_candidate_count = 0
    rows: list[dict[str, Any]] = []

    for row in _rows(db, min_amount=min_amount):
        status = str(row["subscriber_status"] or "")
        if status not in counts:
            continue
        current_available = _money(row["current_available"])
        deposit = _money(row["deposit"])
        open_ar = _money(row["open_ar"])
        subscriber_is_active = bool(row["subscriber_is_active"])
        counts[status] += 1
        totals[status] += current_available
        if not subscriber_is_active:
            soft_deleted_count += 1
            soft_deleted_total += current_available
        if int(row["active_sibling_count"] or 0) > 0:
            sibling_candidate_count += 1
        if current_available >= material_amount:
            material_counts[status] += 1
        rows.append(
            {
                "account_id": str(row["account_id"]),
                "subscriber_name": str(row["subscriber_name"] or ""),
                "subscriber_status": status,
                "subscriber_is_active": subscriber_is_active,
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
                "active_sibling_count": int(row["active_sibling_count"] or 0),
                "active_sibling_account_ids": str(
                    row["active_sibling_account_ids"] or ""
                ),
                "active_sibling_names": str(row["active_sibling_names"] or ""),
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
    refund_review_count = sum(counts[status] for status in REFUND_REVIEW_STATUSES)
    refund_review_total = sum(
        (totals[status] for status in REFUND_REVIEW_STATUSES), Decimal("0.00")
    )
    return {
        "ok": refund_review_count == 0,
        "inactive_positive_count": sum(counts.values()),
        "inactive_positive_total": str(round_money(inactive_positive_total)),
        "disabled_count": counts["disabled"],
        "disabled_total": str(round_money(totals["disabled"])),
        "canceled_count": counts["canceled"],
        "canceled_total": str(round_money(totals["canceled"])),
        "blocked_count": counts["blocked"],
        "blocked_total": str(round_money(totals["blocked"])),
        "suspended_count": counts["suspended"],
        "suspended_total": str(round_money(totals["suspended"])),
        "refund_review_count": refund_review_count,
        "refund_review_total": str(round_money(refund_review_total)),
        "soft_deleted_count": soft_deleted_count,
        "soft_deleted_total": str(round_money(soft_deleted_total)),
        "sibling_candidate_count": sibling_candidate_count,
        "sibling_sample_limit": SIBLING_SAMPLE_LIMIT,
        "material_amount": str(material_amount),
        "material_count": sum(material_counts.values()),
        "by_status": status_summary,
        "sample_limit": sample_limit,
        "samples": rows[:sample_limit],
    }
