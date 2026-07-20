#!/usr/bin/env python3
"""Link unassigned UISP ONTs to customer accounts by exact normalized name.

Safety scope:
- ONT must be active, UISP-managed, named, and have no active assignment.
- ONT normalized name must match exactly one subscriber normalized display name.
- Uses the normal ont_assignments service so CPE inventory side effects run.
- Dry-run by default; --apply writes.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import SessionLocal
from app.schemas.network import OntAssignmentCreate
from app.services.network import ont_assignments

SQL = """
WITH unassigned_onts AS (
    SELECT
        ou.id,
        ou.name,
        ou.serial_number,
        ou.pon_port_id,
        ou.olt_status,
        lower(regexp_replace(coalesce(ou.name, ''), '[^a-zA-Z0-9]+', '', 'g'))
            AS norm_name
    FROM ont_units ou
    WHERE ou.is_active IS TRUE
      AND ou.uisp_device_id IS NOT NULL
      AND coalesce(ou.name, '') <> ''
      AND NOT EXISTS (
          SELECT 1
          FROM ont_assignments oa
          WHERE oa.ont_unit_id = ou.id
            AND oa.is_active IS TRUE
      )
),
subscribers_norm AS (
    SELECT
        s.id,
        s.account_number,
        s.display_name,
        s.status,
        lower(regexp_replace(
            coalesce(
                nullif(s.display_name, ''),
                nullif(s.company_name, ''),
                concat_ws(' ', s.first_name, s.last_name)
            ),
            '[^a-zA-Z0-9]+',
            '',
            'g'
        )) AS norm_name
    FROM subscribers s
    WHERE coalesce(
        nullif(s.display_name, ''),
        nullif(s.company_name, ''),
        concat_ws(' ', s.first_name, s.last_name)
    ) <> ''
),
matches AS (
    SELECT
        u.id AS ont_id,
        u.name AS ont_name,
        u.serial_number,
        u.pon_port_id,
        u.olt_status,
        s.id AS subscriber_id,
        s.account_number,
        s.display_name,
        s.status AS subscriber_status,
        count(*) OVER (PARTITION BY u.id) AS ont_match_count
    FROM unassigned_onts u
    JOIN subscribers_norm s ON s.norm_name = u.norm_name
    WHERE length(u.norm_name) >= 6
)
SELECT *
FROM matches
WHERE ont_match_count = 1
ORDER BY display_name, serial_number
"""


def _rows(db) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(text(SQL)).mappings()]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "result",
        "account_number",
        "display_name",
        "subscriber_status",
        "subscriber_id",
        "ont_id",
        "serial_number",
        "ont_name",
        "olt_status",
        "pon_port_id",
        "assignment_id",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(*, apply: bool, out: Path) -> dict[str, Any]:
    db = SessionLocal()
    evidence: list[dict[str, Any]] = []
    try:
        rows = _rows(db)
        for row in rows:
            result = "dry_run"
            assignment_id = ""
            if apply:
                payload = OntAssignmentCreate(
                    ont_unit_id=row["ont_id"],
                    pon_port_id=row["pon_port_id"],
                    account_id=row["subscriber_id"],
                    active=True,
                    notes=(
                        "Linked by exact UISP ONT name match on 2026-07-07; "
                        f"ONT name={row['ont_name']}; account={row['account_number']}."
                    ),
                )
                assignment = ont_assignments.create(db, payload)
                result = "linked"
                assignment_id = str(assignment.id)
            evidence.append(
                {
                    "result": result,
                    "account_number": row["account_number"],
                    "display_name": row["display_name"],
                    "subscriber_status": row["subscriber_status"],
                    "subscriber_id": str(row["subscriber_id"]),
                    "ont_id": str(row["ont_id"]),
                    "serial_number": row["serial_number"],
                    "ont_name": row["ont_name"],
                    "olt_status": row["olt_status"],
                    "pon_port_id": str(row["pon_port_id"] or ""),
                    "assignment_id": assignment_id,
                }
            )
        if not apply:
            db.rollback()
        _write_csv(out, evidence)
        return {"mode": "APPLY" if apply else "DRY-RUN", "rows": len(rows)}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/exact_name_uisp_ont_links.csv"),
    )
    args = parser.parse_args()
    result = run(apply=args.apply, out=args.out)
    print(f"{result['mode']}: rows={result['rows']}")
    print(f"evidence_csv={args.out}")


if __name__ == "__main__":
    main()
