#!/usr/bin/env python3
"""Maps §B vendor-route drift checker — gate to retiring the PR13 relay
(13-maps-vendor-routes.md §A/§B).

Read-only comparison of the CRM vendor route domain (vendors, vendor_users,
installation_projects, project_quotes, quote_line_items, as_built_routes,
as_built_line_items, proposed_route_revisions, installation_project_notes)
against the native sub tables written by ``import_crm_vendor_routes.py``. Rows
join on the shared CRM UUID (the CRM id is the sub PK), so the PK itself is the
join — no marker metadata to trust.

Finding classes:
  * ``crm_missing_in_sub`` — CRM rows with no sub row of the same id. An
    ``installation_projects`` row whose ``project_id`` is absent from sub's
    native ``projects`` is tagged ``native_project_absent`` (the importer's
    hard blocker); it still gates.
  * ``sub_orphans`` — sub rows whose id no longer exists in CRM.
  * ``field_drift`` — per-table scalar field lists, plus the collapsed
    ``installation_projects.subscriber_id`` link and the ``route_geom``
    LINESTRING (compared as EWKT on both sides, so an identical geometry with
    the same SRID compares equal).
  * ``children_count_mismatch`` — quotes per installation-project, as-built
    routes per project, line items per quote, route revisions per quote,
    as-built line items per as-built route (count + amount sum for line tables).
  * ``sub_enrichment`` — informational: a resolved ``subscriber_id`` where CRM
    held none (non-gating).
  * ``unresolved_subscribers`` / ``unmapped_staff`` — informational orphan CSVs.

Rows whose CRM ``updated_at`` (``created_at`` for the insert-only revision /
note tables) falls within ``--updated-within-minutes`` (default 30) count as
``expected_in_flight`` — webhooks + reconcile keep the window live pre-flip —
and do not gate.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Both sessions are forced READ ONLY and rolled back; the checker never writes.
Output: summary JSON on stdout plus one CSV per class in ``--out``. Exit 0 when
there is zero gating drift outside the live window, 1 otherwise — cron/CI gates
the flip on two consecutive clean runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.migration.check_crm_ticket_drift import in_live_window  # noqa: E402
from scripts.migration.import_crm_tickets_phase1 import (  # noqa: E402
    _engine_from_env,
    _format_datetime,
    _load_staff_map,
    _load_subscriber_map,
    _parse_datetime,
    _rows,
    _uuid_or_none,
)
from scripts.migration.import_crm_vendor_routes import (  # noqa: E402
    STAFF_UUID_COLUMNS,
    geom_read_expr,
)

DEFAULT_UPDATED_WITHIN_MINUTES = 30

GATING_CLASSES = (
    "crm_missing_in_sub",
    "sub_orphans",
    "field_drift",
    "children_count_mismatch",
)

INFO_CLASSES = (
    "sub_enrichment",
    "expected_in_flight",
    "unresolved_subscribers",
    "unmapped_staff",
)

ALL_CLASSES = GATING_CLASSES + INFO_CLASSES

# (table, live-window timestamp column) — insert-only tables watermark on
# created_at, the rest on updated_at.
ID_SET_TABLES: tuple[tuple[str, str], ...] = (
    ("vendors", "updated_at"),
    ("vendor_users", "updated_at"),
    ("installation_projects", "updated_at"),
    ("project_quotes", "updated_at"),
    ("project_quote_line_items", "updated_at"),
    ("as_built_routes", "updated_at"),
    ("as_built_line_items", "updated_at"),
    ("proposed_route_revisions", "created_at"),
    ("installation_project_notes", "created_at"),
)

# CRM source table name per sub table (the line-item table is renamed).
CRM_TABLE_NAME = {
    "project_quote_line_items": "quote_line_items",
}


def _crm_source(table: str) -> str:
    return CRM_TABLE_NAME.get(table, table)


# ---------------------------------------------------------------------------
# Normalisers + field comparison (pure)
# ---------------------------------------------------------------------------


def _norm_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _norm_uuid(value: Any) -> str | None:
    normalized = _uuid_or_none(value)
    return normalized.lower() if normalized else None


def _norm_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _norm_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm_ts(value: Any) -> str | None:
    return _format_datetime(_parse_datetime(value))


@dataclass(frozen=True)
class FieldDiff:
    field: str
    crm_value: str | None
    sub_value: str | None


@dataclass(frozen=True)
class RowComparison:
    diffs: tuple[FieldDiff, ...] = ()
    enrichments: tuple[FieldDiff, ...] = ()
    unresolved_subscriber_reason: str | None = None


# table -> ((field, kind), ...). kind: text | uuid | decimal | int | bool | geom
FIELD_SPECS: dict[str, tuple[tuple[str, str], ...]] = {
    "vendors": (
        ("name", "text"),
        ("code", "text"),
        ("contact_email", "text"),
        ("is_active", "bool"),
    ),
    "vendor_users": (
        ("vendor_id", "uuid"),
        ("person_id", "uuid"),
        ("role", "text"),
        ("is_active", "bool"),
    ),
    "installation_projects": (
        ("project_id", "uuid"),
        ("status", "text"),
        ("assignment_type", "text"),
        ("assigned_vendor_id", "uuid"),
        ("is_active", "bool"),
    ),
    "project_quotes": (
        ("project_id", "uuid"),
        ("vendor_id", "uuid"),
        ("status", "text"),
        ("currency", "text"),
        ("subtotal", "decimal"),
        ("tax_total", "decimal"),
        ("total", "decimal"),
        ("is_active", "bool"),
    ),
    "project_quote_line_items": (
        ("quote_id", "uuid"),
        ("item_type", "text"),
        ("quantity", "decimal"),
        ("unit_price", "decimal"),
        ("amount", "decimal"),
        ("is_active", "bool"),
    ),
    "as_built_routes": (
        ("project_id", "uuid"),
        ("status", "text"),
        ("variation_type", "text"),
        ("actual_length_meters", "decimal"),
        ("version", "int"),
        ("route_geom", "geom"),
        ("is_active", "bool"),
    ),
    "as_built_line_items": (
        ("as_built_id", "uuid"),
        ("item_type", "text"),
        ("quantity", "decimal"),
        ("unit_price", "decimal"),
        ("amount", "decimal"),
        ("is_active", "bool"),
    ),
    "proposed_route_revisions": (
        ("quote_id", "uuid"),
        ("revision_number", "int"),
        ("status", "text"),
        ("length_meters", "decimal"),
        ("route_geom", "geom"),
    ),
    "installation_project_notes": (
        ("project_id", "uuid"),
        ("is_internal", "bool"),
        ("body", "text"),
    ),
}

_NORMALISERS = {
    "text": _norm_text,
    "uuid": _norm_uuid,
    "decimal": _norm_decimal,
    "int": _norm_int,
    "geom": _norm_text,  # EWKT text — deterministic, carries SRID
    "bool": lambda value: bool(value) if value is not None else None,
}


def compare_fields(
    table: str,
    crm_row: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    subscriber_map: dict[str, str],
) -> RowComparison:
    """Table-driven scalar comparison (§B field lists), plus the collapsed
    ``installation_projects.subscriber_id`` link resolution."""
    diffs: list[FieldDiff] = []
    enrichments: list[FieldDiff] = []
    for field_name, kind in FIELD_SPECS.get(table, ()):  # noqa: B007
        norm = _NORMALISERS[kind]
        crm_value = norm(crm_row.get(field_name))
        sub_value = norm(sub_row.get(field_name))
        if crm_value != sub_value:
            diffs.append(
                FieldDiff(
                    field_name,
                    None if crm_value is None else str(crm_value),
                    None if sub_value is None else str(sub_value),
                )
            )

    unresolved = None
    if table == "installation_projects":
        crm_subscriber_id = _uuid_or_none(crm_row.get("subscriber_id"))
        sub_subscriber_id = _norm_uuid(sub_row.get("subscriber_id"))
        if crm_subscriber_id is None:
            if sub_subscriber_id is not None:
                enrichments.append(
                    FieldDiff("subscriber_id", None, sub_subscriber_id)
                )
        else:
            mapped = subscriber_map.get(crm_subscriber_id)
            if mapped:
                if mapped.lower() != sub_subscriber_id:
                    diffs.append(
                        FieldDiff("subscriber_id", mapped.lower(), sub_subscriber_id)
                    )
            else:
                unresolved = "unmapped_crm_subscriber"
    return RowComparison(tuple(diffs), tuple(enrichments), unresolved)


def classify_missing_row(
    table: str, crm_row: dict[str, Any], *, project_ids: set[str]
) -> str:
    """Reason tag for a CRM row absent from sub. ``installation_projects``
    whose native ``project_id`` is absent is the importer's hard blocker; it
    still gates but is tagged so the operator can act on the native gap."""
    if table == "installation_projects":
        project_id = _norm_uuid(crm_row.get("project_id"))
        if not project_id or project_id not in project_ids:
            return "native_project_absent"
    return "missing"


def compare_children_counts(
    crm_counts: dict[str, Any], sub_counts: dict[str, Any], kinds: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    """``(kind, crm_value, sub_value)`` per mismatched aggregate. ``*_sum``
    kinds compare as decimals, the rest as ints."""
    mismatches: list[tuple[str, str, str]] = []
    for kind in kinds:
        crm_value: Decimal | int
        sub_value: Decimal | int
        if kind.endswith("_sum"):
            crm_value = _norm_decimal(crm_counts.get(kind)) or Decimal(0)
            sub_value = _norm_decimal(sub_counts.get(kind)) or Decimal(0)
        else:
            crm_value = int(crm_counts.get(kind) or 0)
            sub_value = int(sub_counts.get(kind) or 0)
        if crm_value != sub_value:
            mismatches.append((kind, str(crm_value), str(sub_value)))
    return mismatches


# ---------------------------------------------------------------------------
# Loaders (monkeypatched in tests)
# ---------------------------------------------------------------------------

_FIELD_SELECT: dict[str, str] = {
    "vendors": "id::text, name, code, contact_email, is_active",
    "vendor_users": "id::text, vendor_id::text, person_id::text, role, is_active",
    "installation_projects": (
        "id::text, project_id::text, subscriber_id::text, assigned_vendor_id::text, "
        "assignment_type::text, status::text, is_active"
    ),
    "project_quotes": (
        "id::text, project_id::text, vendor_id::text, status::text, currency, "
        "subtotal, tax_total, total, is_active"
    ),
    "project_quote_line_items": (
        "id::text, quote_id::text, item_type, quantity, unit_price, amount, is_active"
    ),
    "as_built_routes": (
        "id::text, project_id::text, status::text, variation_type::text, "
        "actual_length_meters, version, is_active"
    ),
    "as_built_line_items": (
        "id::text, as_built_id::text, item_type, quantity, unit_price, amount, is_active"
    ),
    "proposed_route_revisions": (
        "id::text, quote_id::text, revision_number, status::text, length_meters"
    ),
    "installation_project_notes": (
        "id::text, project_id::text, is_internal, body"
    ),
}

# route_geom read as EWKT (SRID inline) on both sides for a deterministic diff.
_GEOM_TABLES = {"as_built_routes", "proposed_route_revisions"}


def _select_columns(table: str, ts_column: str | None) -> str:
    columns = _FIELD_SELECT[table]
    if table in _GEOM_TABLES:
        columns += f", {geom_read_expr('route_geom')}"
    if ts_column:
        columns += f", {ts_column}"
    return columns


def _load_crm_tables(crm: Connection) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, ts_column in ID_SET_TABLES:
        columns = _select_columns(table, ts_column)
        source = _crm_source(table)
        tables[table] = _rows(crm, f"SELECT {columns} FROM {source}")  # noqa: S608
    return tables


def _load_sub_tables(sub: Connection) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, _ in ID_SET_TABLES:
        columns = _select_columns(table, None)
        tables[table] = _rows(sub, f"SELECT {columns} FROM {table}")  # noqa: S608
    return tables


def _count_by_parent(
    conn: Connection, table: str, parent: str, *, with_amount: bool = False
) -> dict[str, dict[str, Any]]:
    amount_select = ", COALESCE(sum(amount), 0) AS amount_sum" if with_amount else ""
    rows = _rows(
        conn,
        f"""
        SELECT {parent}::text AS parent_id, count(*) AS n{amount_select}
        FROM {table}
        GROUP BY {parent}
        """,  # noqa: S608
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry: dict[str, Any] = {"n": int(row["n"])}
        if with_amount:
            entry["amount_sum"] = row["amount_sum"]
        result[str(row["parent_id"]).lower()] = entry
    return result


def _load_child_counts(conn: Connection, *, crm: bool) -> dict[str, dict[str, Any]]:
    """Per-parent child aggregates keyed ``<parent_table>:<parent_id>``."""
    line_table = "quote_line_items" if crm else "project_quote_line_items"
    counts: dict[str, dict[str, Any]] = {}

    def _merge(prefix: str, agg: dict[str, dict[str, Any]], kind: str) -> None:
        for parent_id, entry in agg.items():
            bucket = counts.setdefault(f"{prefix}:{parent_id}", {})
            bucket[kind] = entry["n"]
            if "amount_sum" in entry:
                bucket[f"{kind}_amount_sum"] = entry["amount_sum"]

    _merge(
        "installation_projects",
        _count_by_parent(conn, "project_quotes", "project_id"),
        "quotes",
    )
    _merge(
        "installation_projects",
        _count_by_parent(conn, "as_built_routes", "project_id"),
        "as_built_routes",
    )
    _merge(
        "project_quotes",
        _count_by_parent(conn, line_table, "quote_id", with_amount=True),
        "lines",
    )
    _merge(
        "project_quotes",
        _count_by_parent(conn, "proposed_route_revisions", "quote_id"),
        "route_revisions",
    )
    _merge(
        "as_built_routes",
        _count_by_parent(conn, "as_built_line_items", "as_built_id", with_amount=True),
        "lines",
    )
    return counts


# child kinds per parent table (for the mismatch pass).
CHILD_KINDS: dict[str, tuple[str, ...]] = {
    "installation_projects": ("quotes", "as_built_routes"),
    "project_quotes": ("lines", "lines_amount_sum", "route_revisions"),
    "as_built_routes": ("lines", "lines_amount_sum"),
}


# ---------------------------------------------------------------------------
# Drift run
# ---------------------------------------------------------------------------


def run_drift_check(
    *,
    sub: Connection,
    crm: Connection,
    window_minutes: int,
    staff_map: dict[str, str],
    subscriber_map: dict[str, str],
    project_ids: set[str],
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    now = now or datetime.now(UTC)

    crm_tables = _load_crm_tables(crm)
    sub_tables = _load_sub_tables(sub)

    classes: dict[str, list[dict[str, Any]]] = {name: [] for name in ALL_CLASSES}
    in_flight: dict[str, list[str]] = {}
    table_counts: dict[str, dict[str, int]] = {}

    def _note_in_flight(table: str, row_id: str, finding: str) -> None:
        in_flight.setdefault(f"{table}:{row_id}", []).append(finding)

    sub_by_table: dict[str, dict[str, dict[str, Any]]] = {
        table: {str(row["id"]).lower(): row for row in rows}
        for table, rows in sub_tables.items()
    }

    # ---- generic id-set pass (missing / orphans) --------------------------
    for table, ts_column in ID_SET_TABLES:
        crm_rows = crm_tables.get(table, [])
        sub_rows = sub_by_table.get(table, {})
        crm_ids = {str(row["id"]).lower() for row in crm_rows}
        table_counts[table] = {"crm": len(crm_rows), "sub": len(sub_rows)}
        for row in crm_rows:
            row_id = str(row["id"]).lower()
            if row_id in sub_rows:
                continue
            ts = _parse_datetime(row.get(ts_column))
            in_window = in_live_window(ts, now=now, window_minutes=window_minutes)
            reason = classify_missing_row(table, row, project_ids=project_ids)
            classes["crm_missing_in_sub"].append(
                {
                    "table": table,
                    "crm_id": row_id,
                    "reason": reason,
                    "crm_ts": _format_datetime(ts),
                    "in_live_window": in_window,
                }
            )
            if in_window:
                _note_in_flight(table, row_id, "missing")
        for row_id in sub_rows:
            if row_id not in crm_ids:
                classes["sub_orphans"].append({"table": table, "sub_id": row_id})

    # ---- per-table field comparisons --------------------------------------
    def _window(crm_row: dict[str, Any], ts_column: str) -> bool:
        return in_live_window(
            _parse_datetime(crm_row.get(ts_column)),
            now=now,
            window_minutes=window_minutes,
        )

    for table, ts_column in ID_SET_TABLES:
        for crm_row in crm_tables.get(table, []):
            row_id = str(crm_row["id"]).lower()
            sub_row = sub_by_table[table].get(row_id)
            if sub_row is None:
                continue
            comparison = compare_fields(
                table, crm_row, sub_row, subscriber_map=subscriber_map
            )
            in_window = _window(crm_row, ts_column)
            for diff in comparison.diffs:
                classes["field_drift"].append(
                    {
                        "table": table,
                        "crm_id": row_id,
                        "field": diff.field,
                        "crm_value": diff.crm_value,
                        "sub_value": diff.sub_value,
                        "in_live_window": in_window,
                    }
                )
                if in_window:
                    _note_in_flight(table, row_id, f"field:{diff.field}")
            for enrichment in comparison.enrichments:
                classes["sub_enrichment"].append(
                    {
                        "table": table,
                        "crm_id": row_id,
                        "field": enrichment.field,
                        "crm_value": enrichment.crm_value,
                        "sub_value": enrichment.sub_value,
                    }
                )
            if comparison.unresolved_subscriber_reason:
                classes["unresolved_subscribers"].append(
                    {
                        "table": table,
                        "crm_id": row_id,
                        "reason": comparison.unresolved_subscriber_reason,
                    }
                )

    # ---- children aggregates ----------------------------------------------
    crm_children = _load_child_counts(crm, crm=True)
    sub_children = _load_child_counts(sub, crm=False)
    ts_by_table = dict(ID_SET_TABLES)
    for parent_table, kinds in CHILD_KINDS.items():
        ts_column = ts_by_table[parent_table]
        for crm_row in crm_tables.get(parent_table, []):
            row_id = str(crm_row["id"]).lower()
            if row_id not in sub_by_table[parent_table]:
                continue
            in_window = _window(crm_row, ts_column)
            key = f"{parent_table}:{row_id}"
            for kind, crm_value, sub_value in compare_children_counts(
                crm_children.get(key, {}), sub_children.get(key, {}), kinds
            ):
                classes["children_count_mismatch"].append(
                    {
                        "table": parent_table,
                        "crm_id": row_id,
                        "child": kind,
                        "crm_value": crm_value,
                        "sub_value": sub_value,
                        "in_live_window": in_window,
                    }
                )
                if in_window:
                    _note_in_flight(parent_table, row_id, f"children:{kind}")

    # ---- unmapped staff (informational) -----------------------------------
    staff_seen: dict[tuple[str, str], set[str]] = {}
    for table, column in STAFF_UUID_COLUMNS:
        for row in crm_tables.get(table, []):
            value = _norm_uuid(row.get(column))
            if value:
                staff_seen.setdefault((table, column), set()).add(value)
    for (table, column), person_ids in sorted(staff_seen.items()):
        for person_id in sorted(person_ids):
            if person_id not in staff_map:
                classes["unmapped_staff"].append(
                    {"table": table, "column": column, "crm_person_id": person_id}
                )

    for key, findings in sorted(in_flight.items()):
        table, _, row_id = key.partition(":")
        classes["expected_in_flight"].append(
            {"table": table, "crm_id": row_id, "findings": "|".join(findings)}
        )

    drift_counts = {
        "crm_missing_in_sub": sum(
            1 for row in classes["crm_missing_in_sub"] if not row["in_live_window"]
        ),
        "sub_orphans": len(classes["sub_orphans"]),
        "field_drift": sum(
            1 for row in classes["field_drift"] if not row["in_live_window"]
        ),
        "children_count_mismatch": sum(
            1 for row in classes["children_count_mismatch"] if not row["in_live_window"]
        ),
    }
    summary = {
        "checked_at": _format_datetime(now),
        "updated_within_minutes": window_minutes,
        "table_counts": table_counts,
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
    parser.add_argument("--out", default="vendor-routes-drift-report")
    parser.add_argument(
        "--updated-within-minutes",
        type=int,
        default=DEFAULT_UPDATED_WITHIN_MINUTES,
        help=(
            "CRM rows updated within this window count as expected_in_flight "
            "(webhooks/reconcile still live), not drift."
        ),
    )
    parser.add_argument(
        "--staff-map",
        help="staff_map.csv; staff UUIDs in it leave the unmapped_staff class.",
    )
    parser.add_argument("--limit-csv", type=int, default=50000)
    args = parser.parse_args()

    staff_map = _load_staff_map(args.staff_map)
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
                staff_map=staff_map,
                subscriber_map=_load_subscriber_map(sub),
                project_ids={
                    str(row["id"]).lower()
                    for row in _rows(sub, "SELECT id::text AS id FROM projects")
                },
            )
        finally:
            sub.rollback()
            crm.rollback()

    for name, rows in classes.items():
        _write_csv(out_dir / f"{name}.csv", rows, max(1, args.limit_csv))

    exit_code = 0 if summary["drift_total"] == 0 else 1
    report = {
        **summary,
        "staff_map": args.staff_map,
        "staff_map_entries": len(staff_map),
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
