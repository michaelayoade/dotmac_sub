"""Read-only funded inactive account exposure audit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.services.billing_profile import resolve_billing_profiles
from app.services.common import round_money
from app.services.customer_financial_position import prepaid_available_balances

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


def _candidate_account_ids(db: Session) -> list[UUID]:
    return list(
        db.scalars(
            select(Subscriber.id).where(Subscriber.status.in_(INACTIVE_STATUSES))
        ).all()
    )


def _rows(db: Session, account_ids: list[UUID]):
    if not account_ids:
        return ()
    return db.execute(
        text(
            """
            WITH ticket_counts AS (
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
                       COALESCE(tc.ticket_count, 0) AS ticket_count,
                       COALESCE(se.status_event_count, 0) AS status_event_count,
                       se.latest_status_event_at,
                       s.updated_at
                FROM subscribers s
                LEFT JOIN ticket_counts tc ON tc.subscriber_id = s.id
                LEFT JOIN status_events se ON se.subscriber_id = s.id
                WHERE s.status IN ('blocked', 'disabled', 'suspended', 'canceled')
                  AND s.id IN :account_ids
            )
            SELECT fe.account_id,
                   fe.subscriber_name,
                   fe.subscriber_status,
                   fe.subscriber_is_active,
                   fe.splynx_customer_id,
                   fe.ticket_count,
                   fe.status_event_count,
                   fe.latest_status_event_at,
                   COALESCE(sc.active_sibling_count, 0) AS active_sibling_count,
                   sc.active_sibling_account_ids,
                   sc.active_sibling_names,
                   fe.updated_at
            FROM exposure fe
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
            ORDER BY fe.subscriber_name ASC
            """
        ).bindparams(bindparam("account_ids", expanding=True)),
        {
            "account_ids": account_ids,
            "sibling_sample_limit": SIBLING_SAMPLE_LIMIT,
        },
    ).mappings()


def _canonical_prepaid_balances(
    db: Session, account_ids: list[UUID]
) -> tuple[dict[UUID, Decimal], int, int]:
    accounts = list(
        db.scalars(select(Subscriber).where(Subscriber.id.in_(account_ids))).all()
    )
    profiles = resolve_billing_profiles(db, accounts)
    prepaid_ids: list[UUID] = []
    invalid_profile_count = 0
    non_prepaid_count = 0
    for account_id in account_ids:
        profile = profiles.get(account_id)
        if profile is None or not profile.is_valid:
            invalid_profile_count += 1
        elif profile.effective_mode == BillingMode.prepaid:
            prepaid_ids.append(account_id)
        else:
            non_prepaid_count += 1
    return (
        prepaid_available_balances(db, prepaid_ids),
        invalid_profile_count,
        non_prepaid_count,
    )


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
    """Summarize inactive prepaid accounts with verified positive funding."""

    totals: dict[str, Decimal] = {
        status: Decimal("0.00") for status in INACTIVE_STATUSES
    }
    counts: dict[str, int] = dict.fromkeys(INACTIVE_STATUSES, 0)
    material_counts: dict[str, int] = dict.fromkeys(INACTIVE_STATUSES, 0)
    soft_deleted_count = 0
    soft_deleted_total = Decimal("0.00")
    sibling_candidate_count = 0
    rows: list[dict[str, Any]] = []
    account_ids = _candidate_account_ids(db)
    balances, invalid_profile_count, non_prepaid_count = _canonical_prepaid_balances(
        db, account_ids
    )
    funded_account_ids = [
        account_id for account_id, balance in balances.items() if balance > min_amount
    ]
    candidate_rows = list(_rows(db, funded_account_ids))

    for row in candidate_rows:
        status = str(row["subscriber_status"] or "")
        if status not in counts:
            continue
        current_available = balances.get(UUID(str(row["account_id"])))
        if current_available is None:
            continue
        current_available = _money(current_available)
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
                "funding_source": "verified_prepaid_funding",
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
        "candidate_count": len(account_ids),
        "invalid_billing_profile_count": invalid_profile_count,
        "non_prepaid_candidate_count": non_prepaid_count,
        "funding_source": "verified_prepaid_funding",
        "sibling_sample_limit": SIBLING_SAMPLE_LIMIT,
        "material_amount": str(material_amount),
        "material_count": sum(material_counts.values()),
        "by_status": status_summary,
        "sample_limit": sample_limit,
        "samples": rows[:sample_limit],
    }
