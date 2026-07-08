#!/usr/bin/env python3
"""Read-only preflight for importing CRM tickets into sub support tables.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Outputs one summary JSON file plus one CSV per finding class. This script does
not write to either database; both sessions are placed in read-only mode.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


VALID_STATUSES = {
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
    "pending_confirmation",
    "resolved",
    "closed",
    "canceled",
    "merged",
}
VALID_PRIORITIES = {"lower", "low", "medium", "normal", "high", "urgent"}
VALID_CHANNELS = {"web", "email", "phone", "chat", "api"}

SUB_CRM_ID_SQL = """
SELECT crm_subscriber_id::text AS crm_subscriber_id
FROM subscribers
WHERE crm_subscriber_id::text = ANY(:ids)
UNION
SELECT alias.crm_subscriber_id
FROM subscribers s
CROSS JOIN LATERAL json_array_elements_text(
    CASE
        WHEN json_typeof(s.metadata->'crm_alias_ids') = 'array'
        THEN s.metadata->'crm_alias_ids'
        ELSE '[]'::json
    END
) AS alias(crm_subscriber_id)
WHERE alias.crm_subscriber_id = ANY(:ids)
"""

ROLE_FIELDS = [
    "created_by_person_id",
    "assigned_to_person_id",
    "ticket_manager_person_id",
    "assistant_manager_person_id",
]


@dataclass(frozen=True)
class Finding:
    name: str
    severity: str
    description: str
    rows: list[dict[str, Any]]


def _engine_from_env(name: str) -> Engine:
    url = os.environ.get(name)
    if not url:
        raise SystemExit(f"{name} is required")
    return create_engine(url, pool_pre_ping=True)


def _rows(
    conn: Connection, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = conn.execute(text(sql), params or {})
    return [dict(row._mapping) for row in result]


def _scalar(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> int:
    value = conn.execute(text(sql), params or {}).scalar()
    return int(value or 0)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _limited(sql: str, limit: int) -> str:
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {int(limit)}"


def _literal_list(values: set[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


def collect_findings(
    *,
    sub: Connection,
    crm: Connection,
    sample_limit: int,
) -> tuple[dict[str, int], list[Finding]]:
    summary = {
        "crm_tickets": _scalar(crm, "SELECT count(*) FROM tickets"),
        "crm_ticket_comments": _scalar(crm, "SELECT count(*) FROM ticket_comments"),
        "crm_ticket_assignees": _scalar(crm, "SELECT count(*) FROM ticket_assignees"),
        "crm_ticket_links": _scalar(crm, "SELECT count(*) FROM ticket_links"),
        "crm_ticket_merges": _scalar(crm, "SELECT count(*) FROM ticket_merges"),
        "crm_ticket_access_tokens": _scalar(
            crm, "SELECT count(*) FROM ticket_access_tokens"
        ),
        "crm_active_ticket_access_tokens": _scalar(
            crm, "SELECT count(*) FROM ticket_access_tokens WHERE is_active IS TRUE"
        ),
        "sub_support_tickets": _scalar(sub, "SELECT count(*) FROM support_tickets"),
        "sub_existing_crm_ticket_links": _scalar(
            sub,
            """
            SELECT count(*)
            FROM support_tickets
            WHERE metadata->>'crm_ticket_id' IS NOT NULL
            """,
        ),
    }

    findings: list[Finding] = []

    findings.append(
        Finding(
            name="crm_duplicate_ticket_numbers",
            severity="blocker",
            description=(
                "CRM has duplicate non-null ticket numbers; sub keeps "
                "support_tickets.number unique."
            ),
            rows=_rows(
                crm,
                _limited(
                    """
                    SELECT number,
                           count(*) AS ticket_count,
                           array_agg(id::text ORDER BY created_at) AS ticket_ids
                    FROM tickets
                    WHERE number IS NOT NULL AND btrim(number) <> ''
                    GROUP BY number
                    HAVING count(*) > 1
                    ORDER BY count(*) DESC, number
                    """,
                    sample_limit,
                ),
            ),
        )
    )

    findings.append(
        Finding(
            name="sub_duplicate_crm_ticket_markers",
            severity="blocker",
            description=(
                "Sub already has duplicate metadata.crm_ticket_id markers; "
                "importer dedupe would be ambiguous."
            ),
            rows=_rows(
                sub,
                _limited(
                    """
                    SELECT metadata->>'crm_ticket_id' AS crm_ticket_id,
                           count(*) AS local_count,
                           array_agg(id::text ORDER BY created_at) AS support_ticket_ids
                    FROM support_tickets
                    WHERE metadata->>'crm_ticket_id' IS NOT NULL
                    GROUP BY metadata->>'crm_ticket_id'
                    HAVING count(*) > 1
                    ORDER BY count(*) DESC, crm_ticket_id
                    """,
                    sample_limit,
                ),
            ),
        )
    )

    findings.append(
        Finding(
            name="sub_ticket_number_collisions",
            severity="blocker",
            description=(
                "CRM ticket numbers collide with existing sub tickets not already "
                "linked to that CRM ticket."
            ),
            rows=_rows(
                sub,
                _limited(
                    """
                    SELECT st.number,
                           st.id::text AS support_ticket_id,
                           st.metadata->>'crm_ticket_id' AS existing_crm_ticket_id
                    FROM support_tickets st
                    WHERE st.number IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM support_tickets linked
                          WHERE linked.id = st.id
                            AND linked.metadata->>'crm_ticket_id' IS NOT NULL
                      )
                    ORDER BY st.number
                    """,
                    sample_limit,
                ),
            ),
        )
    )

    crm_numbers = _rows(
        crm,
        """
        SELECT DISTINCT number
        FROM tickets
        WHERE number IS NOT NULL AND btrim(number) <> ''
        """,
    )
    if crm_numbers:
        numbers = [row["number"] for row in crm_numbers]
        collision_rows = _rows(
            sub,
            """
            SELECT st.number,
                   st.id::text AS support_ticket_id,
                   st.metadata->>'crm_ticket_id' AS existing_crm_ticket_id
            FROM support_tickets st
            WHERE st.number = ANY(:numbers)
              AND st.metadata->>'crm_ticket_id' IS NULL
            ORDER BY st.number
            LIMIT :limit
            """,
            {"numbers": numbers, "limit": sample_limit},
        )
        findings[-1] = Finding(
            findings[-1].name,
            findings[-1].severity,
            findings[-1].description,
            collision_rows,
        )

    findings.append(
        Finding(
            name="crm_unknown_ticket_statuses",
            severity="blocker",
            description="CRM has statuses outside the merged status vocabulary.",
            rows=_rows(
                crm,
                _limited(
                    f"""
                    SELECT status::text AS status, count(*) AS ticket_count
                    FROM tickets
                    WHERE status::text NOT IN ({_literal_list(VALID_STATUSES)})
                    GROUP BY status::text
                    ORDER BY ticket_count DESC, status
                    """,
                    sample_limit,
                ),
            ),
        )
    )
    findings.append(
        Finding(
            name="crm_unknown_ticket_priorities",
            severity="blocker",
            description="CRM has priorities outside the merged priority vocabulary.",
            rows=_rows(
                crm,
                _limited(
                    f"""
                    SELECT priority::text AS priority, count(*) AS ticket_count
                    FROM tickets
                    WHERE priority::text NOT IN ({_literal_list(VALID_PRIORITIES)})
                    GROUP BY priority::text
                    ORDER BY ticket_count DESC, priority
                    """,
                    sample_limit,
                ),
            ),
        )
    )
    findings.append(
        Finding(
            name="crm_unknown_ticket_channels",
            severity="blocker",
            description="CRM has channels outside the merged channel vocabulary.",
            rows=_rows(
                crm,
                _limited(
                    f"""
                    SELECT channel::text AS channel, count(*) AS ticket_count
                    FROM tickets
                    WHERE channel::text NOT IN ({_literal_list(VALID_CHANNELS)})
                    GROUP BY channel::text
                    ORDER BY ticket_count DESC, channel
                    """,
                    sample_limit,
                ),
            ),
        )
    )

    findings.append(
        Finding(
            name="crm_unmapped_ticket_subscribers",
            severity="blocker",
            description=(
                "CRM tickets with subscriber_id that cannot map to "
                "sub.subscribers.crm_subscriber_id."
            ),
            rows=_rows(
                crm,
                """
                    SELECT t.id::text AS crm_ticket_id,
                           t.number,
                           t.subscriber_id::text AS crm_subscriber_id,
                           s.external_system,
                           s.external_id,
                           s.person_id::text AS crm_person_id
                    FROM tickets t
                    LEFT JOIN subscribers s ON s.id = t.subscriber_id
                    WHERE t.subscriber_id IS NOT NULL
                    ORDER BY t.created_at
                    """,
            ),
        )
    )
    crm_subscriber_ids = [
        row["crm_subscriber_id"]
        for row in findings[-1].rows
        if row.get("crm_subscriber_id")
    ]
    if crm_subscriber_ids:
        mapped = {
            str(row["crm_subscriber_id"])
            for row in _rows(
                sub,
                SUB_CRM_ID_SQL,
                {"ids": crm_subscriber_ids},
            )
        }
        findings[-1] = Finding(
            findings[-1].name,
            findings[-1].severity,
            findings[-1].description,
            [
                row
                for row in findings[-1].rows
                if str(row.get("crm_subscriber_id")) not in mapped
            ],
        )

    customer_person_rows = _rows(
        crm,
        """
            SELECT t.id::text AS crm_ticket_id,
                   t.number,
                   t.customer_person_id::text AS crm_customer_person_id,
                   p.email,
                   p.metadata->>'selfcare_id' AS selfcare_id,
                   s.id::text AS crm_subscriber_id
            FROM tickets t
            LEFT JOIN people p ON p.id = t.customer_person_id
            LEFT JOIN subscribers s ON s.person_id = t.customer_person_id
            WHERE t.customer_person_id IS NOT NULL
            ORDER BY t.created_at
            """,
    )
    selfcare_ids = [
        row["selfcare_id"] for row in customer_person_rows if row.get("selfcare_id")
    ]
    crm_person_subscriber_ids = [
        row["crm_subscriber_id"]
        for row in customer_person_rows
        if row.get("crm_subscriber_id")
    ]
    mapped_selfcare_ids = set()
    mapped_crm_person_subscriber_ids = set()
    if selfcare_ids:
        mapped_selfcare_ids = {
            str(row["id"])
            for row in _rows(
                sub,
                "SELECT id::text AS id FROM subscribers WHERE id::text = ANY(:ids)",
                {"ids": selfcare_ids},
            )
        }
    if crm_person_subscriber_ids:
        mapped_crm_person_subscriber_ids = {
            str(row["crm_subscriber_id"])
            for row in _rows(
                sub,
                SUB_CRM_ID_SQL,
                {"ids": crm_person_subscriber_ids},
            )
        }
    findings.append(
        Finding(
            name="crm_unmapped_customer_people",
            severity="warning",
            description=(
                "Ticket customer_person_id values that do not resolve through "
                "selfcare_id or CRM subscriber.person_id."
            ),
            rows=[
                row
                for row in customer_person_rows
                if (
                    not row.get("selfcare_id")
                    or str(row["selfcare_id"]) not in mapped_selfcare_ids
                )
                and (
                    not row.get("crm_subscriber_id")
                    or str(row["crm_subscriber_id"])
                    not in mapped_crm_person_subscriber_ids
                )
            ],
        )
    )

    role_union = "\nUNION\n".join(
        f"SELECT {field} AS person_id FROM tickets WHERE {field} IS NOT NULL"
        for field in ROLE_FIELDS
    )
    staff_rows = _rows(
        crm,
        f"""
            WITH role_people AS (
                {role_union}
                UNION
                SELECT person_id FROM ticket_assignees WHERE person_id IS NOT NULL
                UNION
                SELECT merged_by_person_id
                FROM ticket_merges
                WHERE merged_by_person_id IS NOT NULL
                UNION
                SELECT created_by_person_id
                FROM ticket_links
                WHERE created_by_person_id IS NOT NULL
            )
            SELECT rp.person_id::text AS crm_person_id,
                   p.email,
                   p.first_name,
                   p.last_name
            FROM role_people rp
            LEFT JOIN people p ON p.id = rp.person_id
            ORDER BY p.email NULLS LAST, rp.person_id
            """,
    )
    staff_emails = [str(row["email"]).lower() for row in staff_rows if row.get("email")]
    mapped_staff_emails = set()
    if staff_emails:
        mapped_staff_emails = {
            str(row["email"]).lower()
            for row in _rows(
                sub,
                """
                SELECT email
                FROM system_users
                WHERE lower(email) = ANY(:emails)
                """,
                {"emails": staff_emails},
            )
        }
    findings.append(
        Finding(
            name="crm_unmapped_staff_people",
            severity="warning",
            description=(
                "CRM staff role/assignee people that do not map to sub "
                "system_users by email."
            ),
            rows=[
                row
                for row in staff_rows
                if not row.get("email")
                or str(row["email"]).lower() not in mapped_staff_emails
            ],
        )
    )

    findings.append(
        Finding(
            name="crm_duplicate_ticket_links",
            severity="warning",
            description=(
                "CRM ticket_links rows that collapse into sub's unique "
                "from/to/type constraint."
            ),
            rows=_rows(
                crm,
                _limited(
                    """
                    SELECT from_ticket_id::text,
                           to_ticket_id::text,
                           link_type,
                           count(*) AS link_count,
                           array_agg(id::text ORDER BY created_at) AS link_ids
                    FROM ticket_links
                    GROUP BY from_ticket_id, to_ticket_id, link_type
                    HAVING count(*) > 1
                    ORDER BY count(*) DESC
                    """,
                    sample_limit,
                ),
            ),
        )
    )

    findings.append(
        Finding(
            name="crm_ticket_attachment_metadata",
            severity="info",
            description=(
                "CRM tickets with metadata.attachments; importer must move these "
                "to support_tickets.attachments."
            ),
            rows=_rows(
                crm,
                _limited(
                    """
                    SELECT id::text AS crm_ticket_id,
                           number,
                           json_array_length(metadata->'attachments')
                               AS attachment_count
                    FROM tickets
                    WHERE json_typeof(metadata->'attachments') = 'array'
                      AND json_array_length(metadata->'attachments') > 0
                    ORDER BY json_array_length(metadata->'attachments') DESC
                    """,
                    sample_limit,
                ),
            ),
        )
    )

    return summary, findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="ticket-import-preflight")
    parser.add_argument("--sample-limit", type=int, default=500)
    args = parser.parse_args()

    out = Path(args.out)
    sample_limit = max(1, args.sample_limit)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        sub.execute(text("SET TRANSACTION READ ONLY"))
        crm.execute(text("SET TRANSACTION READ ONLY"))
        try:
            summary, findings = collect_findings(
                sub=sub,
                crm=crm,
                sample_limit=sample_limit,
            )
        finally:
            sub.rollback()
            crm.rollback()

    finding_counts = []
    for finding in findings:
        finding_counts.append(
            {
                "name": finding.name,
                "severity": finding.severity,
                "count": len(finding.rows),
                "description": finding.description,
            }
        )
        _write_csv(out / f"{finding.name}.csv", finding.rows)

    blockers = sum(
        item["count"] for item in finding_counts if item["severity"] == "blocker"
    )
    report = {
        "summary": summary,
        "finding_counts": finding_counts,
        "blocker_rows": blockers,
        "output_dir": str(out),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    _write_csv(out / "finding_counts.csv", finding_counts)

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
