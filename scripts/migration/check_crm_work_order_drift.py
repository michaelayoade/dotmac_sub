#!/usr/bin/env python3
"""Phase 2 work-order drift checker — flip gate (12-phase2-completion.md §C2).

Read-only comparison of CRM ``work_orders`` against sub ``work_order_mirror``
joined on ``work_order_mirror.crm_work_order_id`` (03-drift-report /
check_crm_ticket_drift mold):

  * ``crm_missing_in_sub`` — active, OPEN (non-terminal) CRM work orders with
    no mirror row. Completed/canceled CRM work orders are archive posture
    (never required in the mirror) and don't gate.
  * ``sub_orphan_mirror_rows`` — CRM-origin mirror rows (id not
    ``sub-``-prefixed) whose work order no longer exists in CRM at all.
    Native rows (``sub-`` ids) are sub-born and never checked against CRM.
  * ``field_drift`` — per-work-order status + subscriber-link diffs.
    **Native-write tolerance**: rows the sub field services own
    (``metadata.native_field_source == 'sub'``) keep sub's status by design —
    those diffs are reported in the informational, non-gating
    ``native_precedence`` class instead (reconcile-clobber protection means
    sub is authoritative there).
  * ``expected_in_flight`` — CRM rows updated within
    ``--updated-within-minutes`` (default 30): the webhook/reconcile glue is
    still live pre-flip, so these don't gate.
  * ``unresolved_subscribers`` — informational: open CRM work orders whose
    subscriber has no sub mapping (via ``crm_subscriber_id`` +
    ``metadata.crm_alias_ids``).

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Both sessions are forced into READ ONLY transactions and rolled back; the
checker never writes to either database.

Output: summary JSON on stdout plus one CSV per finding class in ``--out``.
Exit code 0 when there is zero drift outside the live window (and outside
native precedence), 1 otherwise, so a cron/CI job can gate the flip on it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.migration.check_crm_ticket_drift import in_live_window  # noqa: E402
from scripts.migration.import_crm_tickets_phase1 import (  # noqa: E402
    _engine_from_env,
    _format_datetime,
    _load_subscriber_map,
    _parse_datetime,
    _rows,
    _uuid_or_none,
)

DEFAULT_UPDATED_WITHIN_MINUTES = 30

# Shared 1:1 vocabulary (CRM WorkOrderStatus == mirror status strings).
TERMINAL_STATUSES = frozenset({"completed", "canceled"})

NATIVE_PREFIX = "sub-"


def is_native_row(crm_work_order_id: str | None) -> bool:
    return str(crm_work_order_id or "").startswith(NATIVE_PREFIX)


def is_open_status(status: str | None) -> bool:
    return (status or "").strip().lower() not in TERMINAL_STATUSES


def classify_status(
    crm_status: str | None,
    sub_status: str | None,
    *,
    native_field_source: str | None,
) -> str:
    """``ok`` | ``native_precedence`` | ``drift`` for one joined work order.

    Native-write tolerance: when sub's field services own the row
    (``native_field_source == 'sub'``), sub's status is authoritative and a
    CRM disagreement is expected (reconcile-clobber protection), not drift.
    """
    crm = (crm_status or "").strip().lower()
    sub = (sub_status or "").strip().lower()
    if crm == sub:
        return "ok"
    if (native_field_source or "").strip().lower() == "sub":
        return "native_precedence"
    return "drift"


def _crm_work_orders(crm: Connection) -> list[dict[str, Any]]:
    return _rows(
        crm,
        """
        SELECT id::text AS id,
               subscriber_id::text AS subscriber_id,
               title,
               status::text AS status,
               is_active,
               completed_at,
               updated_at
        FROM work_orders
        """,
    )


def _sub_mirror_rows(sub: Connection) -> list[dict[str, Any]]:
    return _rows(
        sub,
        """
        SELECT crm_work_order_id,
               subscriber_id::text AS subscriber_id,
               status,
               is_active,
               metadata->>'native_field_source' AS native_field_source,
               updated_at
        FROM work_order_mirror
        """,
    )


def run_drift_check(
    *,
    sub: Connection,
    crm: Connection,
    window_minutes: int,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Run all comparisons; return (summary, per-class CSV rows)."""
    now = now or datetime.now(UTC)

    crm_work_orders = _crm_work_orders(crm)
    mirror_rows = _sub_mirror_rows(sub)
    subscriber_map = _load_subscriber_map(sub)

    mirror_by_crm_id = {str(r["crm_work_order_id"]): r for r in mirror_rows}
    crm_ids = {str(wo["id"]) for wo in crm_work_orders}

    classes: dict[str, list[dict[str, Any]]] = {
        "crm_missing_in_sub": [],
        "sub_orphan_mirror_rows": [],
        "field_drift": [],
        "native_precedence": [],
        "expected_in_flight": [],
        "unresolved_subscribers": [],
    }
    in_flight_findings: dict[str, list[str]] = {}

    open_active_crm = [
        wo
        for wo in crm_work_orders
        if bool(wo.get("is_active")) and is_open_status(wo.get("status"))
    ]

    for wo in open_active_crm:
        crm_wo_id = str(wo["id"])
        crm_updated_at = _parse_datetime(wo.get("updated_at"))
        in_window = in_live_window(
            crm_updated_at, now=now, window_minutes=window_minutes
        )
        mirror = mirror_by_crm_id.get(crm_wo_id)

        crm_subscriber_id = _uuid_or_none(wo.get("subscriber_id"))
        mapped_subscriber = (
            subscriber_map.get(crm_subscriber_id) if crm_subscriber_id else None
        )
        if crm_subscriber_id and not mapped_subscriber:
            classes["unresolved_subscribers"].append(
                {
                    "crm_work_order_id": crm_wo_id,
                    "title": wo.get("title"),
                    "status": wo.get("status"),
                    "crm_subscriber_id": crm_subscriber_id,
                }
            )

        if mirror is None:
            if not mapped_subscriber:
                # No sub subscriber to mirror under — informational only
                # (already counted in unresolved_subscribers).
                continue
            classes["crm_missing_in_sub"].append(
                {
                    "crm_work_order_id": crm_wo_id,
                    "title": wo.get("title"),
                    "status": wo.get("status"),
                    "crm_subscriber_id": crm_subscriber_id,
                    "crm_updated_at": _format_datetime(crm_updated_at),
                    "in_live_window": in_window,
                }
            )
            if in_window:
                in_flight_findings.setdefault(crm_wo_id, []).append("missing")
            continue

        native_field_source = mirror.get("native_field_source")
        outcome = classify_status(
            wo.get("status"),
            mirror.get("status"),
            native_field_source=native_field_source,
        )
        if outcome == "native_precedence":
            classes["native_precedence"].append(
                {
                    "crm_work_order_id": crm_wo_id,
                    "field": "status",
                    "crm_value": wo.get("status"),
                    "sub_value": mirror.get("status"),
                }
            )
        elif outcome == "drift":
            classes["field_drift"].append(
                {
                    "crm_work_order_id": crm_wo_id,
                    "field": "status",
                    "crm_value": wo.get("status"),
                    "sub_value": mirror.get("status"),
                    "in_live_window": in_window,
                }
            )
            if in_window:
                in_flight_findings.setdefault(crm_wo_id, []).append("field:status")

        if mapped_subscriber:
            sub_subscriber = str(mirror.get("subscriber_id") or "").lower()
            if sub_subscriber and sub_subscriber != str(mapped_subscriber).lower():
                classes["field_drift"].append(
                    {
                        "crm_work_order_id": crm_wo_id,
                        "field": "subscriber_id",
                        "crm_value": str(mapped_subscriber).lower(),
                        "sub_value": sub_subscriber,
                        "in_live_window": in_window,
                    }
                )
                if in_window:
                    in_flight_findings.setdefault(crm_wo_id, []).append(
                        "field:subscriber_id"
                    )

    for crm_wo_id, mirror in mirror_by_crm_id.items():
        if is_native_row(crm_wo_id) or crm_wo_id in crm_ids:
            continue
        classes["sub_orphan_mirror_rows"].append(
            {
                "crm_work_order_id": crm_wo_id,
                "subscriber_id": mirror.get("subscriber_id"),
                "status": mirror.get("status"),
                "updated_at": _format_datetime(
                    _parse_datetime(mirror.get("updated_at"))
                ),
            }
        )

    for crm_wo_id, findings in sorted(in_flight_findings.items()):
        classes["expected_in_flight"].append(
            {"crm_work_order_id": crm_wo_id, "findings": "|".join(findings)}
        )

    drift_counts = {
        "crm_missing_in_sub": sum(
            1 for row in classes["crm_missing_in_sub"] if not row["in_live_window"]
        ),
        "sub_orphan_mirror_rows": len(classes["sub_orphan_mirror_rows"]),
        "field_drift": sum(
            1 for row in classes["field_drift"] if not row["in_live_window"]
        ),
    }
    native_rows = sum(1 for r in mirror_by_crm_id if is_native_row(r))
    summary = {
        "checked_at": _format_datetime(now),
        "updated_within_minutes": window_minutes,
        "totals": {
            "crm_work_orders": len(crm_work_orders),
            "crm_open_active": len(open_active_crm),
            "sub_mirror_rows": len(mirror_rows),
            "sub_native_rows": native_rows,
        },
        "classes": {
            name: {"rows": len(rows), "drift": drift_counts.get(name, 0)}
            for name, rows in classes.items()
        },
        "drift_total": sum(drift_counts.values()),
    }
    return summary, classes


def _write_csv(path: Path, rows: list[dict[str, Any]], limit: int) -> None:
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
        writer.writerows(rows[:limit])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="work-order-drift-report")
    parser.add_argument(
        "--updated-within-minutes",
        type=int,
        default=DEFAULT_UPDATED_WITHIN_MINUTES,
        help=(
            "CRM work orders updated within this window count as "
            "expected_in_flight (webhook/reconcile glue still live), not drift."
        ),
    )
    parser.add_argument("--limit-csv", type=int, default=50000)
    args = parser.parse_args()

    out_dir = Path(args.out)
    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        sub.execute(text("SET TRANSACTION READ ONLY"))
        crm.execute(text("SET TRANSACTION READ ONLY"))
        try:
            summary, classes = run_drift_check(
                sub=sub,
                crm=crm,
                window_minutes=args.updated_within_minutes,
            )
        finally:
            sub.rollback()
            crm.rollback()

    for name, rows in classes.items():
        _write_csv(out_dir / f"{name}.csv", rows, max(1, args.limit_csv))

    exit_code = 0 if summary["drift_total"] == 0 else 1
    report = {
        **summary,
        "output_dir": str(out_dir),
        "exit_code": exit_code,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
