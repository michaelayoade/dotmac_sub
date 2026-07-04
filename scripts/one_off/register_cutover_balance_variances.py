#!/usr/bin/env python3
"""Register reviewed cutover balance variances.

Input CSV columns:

    account_id,expected_drift,direction,reason,evidence_ref,approved_by

Dry-run by default. In apply mode each row must still match the current raw
cutover drift exactly before an accepted registry row is inserted.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.db import SessionLocal
from app.services.common import coerce_uuid, round_money
from app.services.cutover_balance_audit import _direction, _rows

TOLERANCE = Decimal("0.01")


def money(value: object) -> Decimal:
    return round_money(Decimal(str(value or 0)))


def raw_drift_map(db) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in _rows(db):
        account_id = str(row["account_id"])
        current = money(row["current_available"])
        target = money(row["target_available"])
        raw_drift = money(current - target)
        output[account_id] = {
            "account_id": account_id,
            "subscriber_name": str(row["subscriber_name"] or ""),
            "subscriber_status": str(row["subscriber_status"] or ""),
            "current_available": current,
            "target_available": target,
            "raw_drift": raw_drift,
            "direction": _direction(raw_drift),
        }
    return output


def existing_active_variances(db) -> set[str]:
    rows = db.execute(
        text(
            """
            SELECT account_id::text
            FROM cutover_balance_variances
            WHERE is_active IS TRUE
              AND status = 'accepted'
            """
        )
    )
    return {str(row[0]) for row in rows}


def load_input(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    required = {
        "account_id",
        "expected_drift",
        "direction",
        "reason",
        "evidence_ref",
        "approved_by",
    }
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        raise SystemExit(f"missing required CSV columns: {sorted(missing)}")
    return rows


def validate_row(
    row: dict[str, str],
    current_raw: dict[str, Any],
    existing: set[str],
) -> dict[str, Any]:
    account_id = str(coerce_uuid(row["account_id"]))
    expected_drift = money(row["expected_drift"])
    direction = row["direction"].strip()
    reason = row["reason"].strip()
    evidence_ref = row["evidence_ref"].strip()
    approved_by = row["approved_by"].strip()
    if direction not in {"overcredited", "understated"}:
        raise RuntimeError(f"{account_id}: invalid direction {direction!r}")
    if expected_drift == Decimal("0.00"):
        raise RuntimeError(f"{account_id}: expected_drift must be non-zero")
    if not reason or not evidence_ref or not approved_by:
        raise RuntimeError(
            f"{account_id}: reason, evidence_ref, and approved_by are required"
        )
    if account_id in existing:
        raise RuntimeError(f"{account_id}: active accepted variance already exists")
    current = current_raw.get(account_id)
    if current is None:
        raise RuntimeError(f"{account_id}: account is not in cutover audit population")
    raw_drift = money(current["raw_drift"])
    if abs(raw_drift - expected_drift) > TOLERANCE:
        raise RuntimeError(
            f"{account_id}: expected_drift {expected_drift} != current raw drift {raw_drift}"
        )
    if current["direction"] != direction:
        raise RuntimeError(
            f"{account_id}: direction {direction} != current direction {current['direction']}"
        )
    return {
        **current,
        "expected_drift": expected_drift,
        "reason": reason,
        "evidence_ref": evidence_ref,
        "approved_by": approved_by,
    }


def write_evidence(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "account_id",
        "subscriber_name",
        "subscriber_status",
        "current_available",
        "target_available",
        "raw_drift",
        "direction",
        "expected_drift",
        "reason",
        "evidence_ref",
        "approved_by",
        "result",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--out",
        default="scratchpad/cutover_balance_variance_registration_dry_run.csv",
    )
    args = parser.parse_args()

    db = SessionLocal()
    evidence: list[dict[str, Any]] = []
    try:
        rows = load_input(Path(args.input))
        current_raw = raw_drift_map(db)
        existing = existing_active_variances(db)
        validated = [validate_row(row, current_raw, existing) for row in rows]
        total = money(
            sum((abs(row["expected_drift"]) for row in validated), Decimal("0"))
        )
        print(
            f"cutover variance registration: rows={len(validated)} total={total} "
            f"mode={'APPLY' if args.apply else 'DRY-RUN'}"
        )
        for row in validated:
            result = "dry_run"
            if args.apply:
                now = datetime.now(UTC)
                db.execute(
                    text(
                        """
                        INSERT INTO cutover_balance_variances (
                            id, account_id, expected_drift, direction, reason,
                            evidence_ref, approved_by, approved_at, status,
                            is_active, created_at, updated_at
                        )
                        VALUES (
                            :id, :account_id, :expected_drift, :direction, :reason,
                            :evidence_ref, :approved_by, :approved_at, 'accepted',
                            TRUE, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "account_id": coerce_uuid(row["account_id"]),
                        "expected_drift": row["expected_drift"],
                        "direction": row["direction"],
                        "reason": row["reason"],
                        "evidence_ref": row["evidence_ref"],
                        "approved_by": row["approved_by"],
                        "approved_at": now,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                result = "registered"
            evidence.append(
                {
                    **{
                        key: str(value)
                        for key, value in row.items()
                        if key
                        in {
                            "account_id",
                            "subscriber_name",
                            "subscriber_status",
                            "current_available",
                            "target_available",
                            "raw_drift",
                            "direction",
                            "expected_drift",
                            "reason",
                            "evidence_ref",
                            "approved_by",
                        }
                    },
                    "result": result,
                }
            )
        if args.apply:
            db.commit()
        else:
            db.rollback()
        write_evidence(Path(args.out), evidence)
        print(f"evidence_csv={args.out}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
