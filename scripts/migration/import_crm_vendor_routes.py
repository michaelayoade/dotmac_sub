#!/usr/bin/env python3
"""Maps §B vendor-route backfill: the CRM vendor installation-project / quote /
route domain into sub's now-native tables (13-maps-vendor-routes.md §A/§B).

The maps §A migration (248) ported ``dotmac_crm/app/models/vendor.py`` natively
into sub (``app/models/vendor_routes.py``) keeping the **CRM UUIDs as sub PKs**,
so every upsert here is an idempotent ``ON CONFLICT (id)`` on the CRM key itself
— no marker metadata to dedupe on (same house rule as ``import_crm_phase3.py``).

Steps run in FK-driven order (§B):

  1. ``vendors``
  2. ``vendor_users``           (vendor_id -> vendors; person_id verbatim UUID)
  3. ``installation_projects``  (project_id -> native ``projects``;
     subscriber_id -> link key 1; buildout_project_id/fiber refs nullable)
  4. ``project_quotes``         (project_id -> installation_projects; vendor_id
     -> vendors)
  5. ``project_quote_line_items`` (CRM ``quote_line_items``; quote_id ->
     project_quotes — renamed to dodge sub's sales ``quote_line_items`` clash)
  6. ``as_built_routes``        (project_id -> installation_projects;
     route_geom LINESTRING; proposed_revision_id deferred — see below)
  7. ``as_built_line_items``    (as_built_id -> as_built_routes)
  8. ``proposed_route_revisions`` (quote_id -> project_quotes; route_geom)
  9. ``installation_project_notes`` (project_id -> installation_projects)

Two mutual/forward FKs cannot be satisfied in the §B step order and are applied
in a final **deferred-link** pass, exactly like phase 3's two-phase
``parent_task_id`` re-link:

  * ``installation_projects.approved_quote_id`` -> ``project_quotes`` (the quote
    is imported in step 4, after the project in step 3);
  * ``as_built_routes.proposed_revision_id`` -> ``proposed_route_revisions``
    (revisions are imported in step 8, after the as-built in step 6).

Both are nullable, so the rows insert with the column NULL and the finalize pass
stamps it once the target exists (missing target -> left NULL + CSV).

Link resolution (§B / FK-clash rules):
  * ``installation_projects.project_id`` is a **NOT NULL** FK to sub
    ``projects.id``. Phase 3 carried CRM project UUIDs verbatim, so it is a
    direct id match; a CRM installation-project pointing at a project absent
    from sub cannot be inserted and **blocks** the run (CSV, exit 2).
  * ``installation_projects.subscriber_id`` resolves through link key 1
    (``subscribers.crm_subscriber_id`` + ``metadata->crm_alias_ids``, the same
    ``_load_subscriber_map`` helper phase 3 uses); nullable, so an unmapped
    subscriber imports unlinked and surfaces in the drift checker.
  * ``buildout_project_id`` -> ``buildout_projects.id`` if present else NULL;
    the route ``fiber_segment_id`` columns -> ``fiber_segments.id`` if present
    else NULL (both nullable both sides).
  * person UUIDs (``vendor_users.person_id``, ``created_by``/``reviewed_by``/
    ``submitted_by``, note authors) carry **verbatim** — sub has no ``people``
    table, they are plain UUID columns. ``address_id`` carries verbatim too.
  * CRM PG-enum columns are read ``::text`` and written into sub's String
    columns verbatim (exact CRM vocabularies).

Geometry passthrough (``route_geom`` LINESTRING, SRID 4326 both sides):
  read ``ST_AsEWKT(route_geom)`` from CRM and write ``ST_GeomFromEWKT(:value)``
  into sub. EWKT carries the SRID inline (``SRID=4326;LINESTRING(...)``) so the
  round-trip preserves both the vertices (full double precision) and the SRID
  with no reprojection — matching how sub stores the identical
  ``Geometry('LINESTRING', srid=4326)`` column on ``FiberSegment``
  (``app/models/network.py``). ``ST_GeomFromEWKT(NULL)`` is NULL, so the wrap is
  unconditional.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Dry-run by default; ``--apply`` writes to sub (the CRM session is always read
only). ``--state-file`` keeps one ``updated_at``/``created_at`` watermark per
CRM table for incremental re-runs while the mirrors + webhooks stay live;
children of re-fetched parents are re-synced even when their own watermark
misses them. ``--staff-map`` only feeds the informational unmapped-staff CSV
(person UUIDs carry verbatim either way). Exit 2 when the run has blockers.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.migration.import_crm_phase3 import (  # noqa: E402
    _load_existing_ids,
    _verbatim_json,
    _write_csv,
    watermark_key,
    write_state_keys,
)
from scripts.migration.import_crm_tickets_phase1 import (  # noqa: E402
    _engine_from_env,
    _format_datetime,
    _load_staff_map,
    _load_subscriber_map,
    _parse_datetime,
    _rows,
    _state_watermark,
    _uuid_or_none,
)

IMPORT_SOURCE = "dotmac_crm_vendor_routes"

# §B FK-driven step order (asserted by the tests). ``deferred_links`` is the
# finalize pass for the two forward/mutual FKs (approved_quote / proposed
# revision) that the table order cannot satisfy inline.
STEP_ORDER = (
    "vendors",
    "vendor_users",
    "installation_projects",
    "project_quotes",
    "project_quote_line_items",
    "as_built_routes",
    "as_built_line_items",
    "proposed_route_revisions",
    "installation_project_notes",
    "deferred_links",
)

REPORT_ACTIONS = (
    "blockers",
    "missing_project",
    "unresolved_subscribers",
    "skipped_unimported_parent",
    "dangling_buildout_project",
    "dangling_fiber_segment",
    "deferred_link_unresolved",
    "unmapped_staff",
)

# (table, column) person UUIDs carried verbatim; feed only the unmapped-staff
# informational report (mirrors phase 3's STAFF_UUID_COLUMNS).
STAFF_UUID_COLUMNS = (
    ("vendor_users", "person_id"),
    ("installation_projects", "created_by_person_id"),
    ("project_quotes", "reviewed_by_person_id"),
    ("project_quotes", "created_by_person_id"),
    ("as_built_routes", "submitted_by_person_id"),
    ("as_built_routes", "reviewed_by_person_id"),
    ("proposed_route_revisions", "submitted_by_person_id"),
    ("proposed_route_revisions", "reviewed_by_person_id"),
    ("installation_project_notes", "author_person_id"),
)


# ---------------------------------------------------------------------------
# Generic upsert SQL (geom-aware variant of the phase 3 builder)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    table: str
    columns: tuple[str, ...]
    uuid_columns: frozenset[str] = frozenset()
    json_columns: frozenset[str] = frozenset()
    geom_columns: frozenset[str] = frozenset()
    conflict_columns: tuple[str, ...] = ("id",)
    immutable_columns: tuple[str, ...] = ("id", "created_at")


def geom_read_expr(column: str) -> str:
    """CRM read side: EWKT carries the SRID inline for a lossless round-trip."""
    return f"ST_AsEWKT({column}) AS {column}"


def build_upsert_sql(spec: TableSpec) -> str:
    def _placeholder(column: str) -> str:
        if column in spec.geom_columns:
            # ST_GeomFromEWKT(NULL) is NULL, so the wrap is unconditional and
            # preserves SRID=4326 verbatim (no reprojection).
            return f"ST_GeomFromEWKT(:{column})"
        if column in spec.uuid_columns:
            return f"CAST(:{column} AS uuid)"
        if column in spec.json_columns:
            return f"CAST(:{column} AS json)"
        return f":{column}"

    columns_sql = ", ".join(spec.columns)
    values_sql = ", ".join(_placeholder(column) for column in spec.columns)
    conflict_sql = ", ".join(spec.conflict_columns)
    updatable = [
        column
        for column in spec.columns
        if column not in spec.conflict_columns and column not in spec.immutable_columns
    ]
    update_sql = ",\n    ".join(
        f"{column} = EXCLUDED.{column}" for column in updatable
    )
    return (
        f"INSERT INTO {spec.table} ({columns_sql})\n"
        f"VALUES ({values_sql})\n"
        f"ON CONFLICT ({conflict_sql}) DO UPDATE SET\n    {update_sql}"
    )


def _spec(
    table: str,
    columns: tuple[str, ...],
    *,
    uuid_columns: frozenset[str] = frozenset(),
    json_columns: frozenset[str] = frozenset(),
    geom_columns: frozenset[str] = frozenset(),
) -> TableSpec:
    return TableSpec(
        table=table,
        columns=columns,
        uuid_columns=frozenset({"id"}) | uuid_columns,
        json_columns=json_columns,
        geom_columns=geom_columns,
    )


TABLE_SPECS: dict[str, TableSpec] = {
    "vendors": _spec(
        "vendors",
        (
            "id",
            "name",
            "code",
            "contact_name",
            "contact_email",
            "contact_phone",
            "license_number",
            "service_area",
            "is_active",
            "notes",
            "erp_id",
            "created_at",
            "updated_at",
        ),
    ),
    "vendor_users": _spec(
        "vendor_users",
        (
            "id",
            "vendor_id",
            "person_id",
            "role",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"vendor_id", "person_id"}),
    ),
    # approved_quote_id deliberately absent — deferred to the finalize pass
    # (the quote is imported after the project).
    "installation_projects": _spec(
        "installation_projects",
        (
            "id",
            "project_id",
            "buildout_project_id",
            "subscriber_id",
            "address_id",
            "assigned_vendor_id",
            "assignment_type",
            "status",
            "bidding_open_at",
            "bidding_close_at",
            "erp_purchase_order_id",
            "created_by_person_id",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {
                "project_id",
                "buildout_project_id",
                "subscriber_id",
                "address_id",
                "assigned_vendor_id",
                "created_by_person_id",
            }
        ),
    ),
    "project_quotes": _spec(
        "project_quotes",
        (
            "id",
            "project_id",
            "vendor_id",
            "status",
            "currency",
            "subtotal",
            "vat_rate_percent",
            "tax_total",
            "total",
            "valid_from",
            "valid_until",
            "submitted_at",
            "reviewed_at",
            "reviewed_by_person_id",
            "review_notes",
            "created_by_person_id",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {"project_id", "vendor_id", "reviewed_by_person_id", "created_by_person_id"}
        ),
    ),
    "project_quote_line_items": _spec(
        "project_quote_line_items",
        (
            "id",
            "quote_id",
            "item_type",
            "description",
            "cable_type",
            "fiber_count",
            "splice_count",
            "quantity",
            "unit_price",
            "amount",
            "notes",
            "client_ref",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"quote_id", "client_ref"}),
    ),
    # proposed_revision_id deliberately absent — deferred to the finalize pass
    # (revisions are imported after the as-built).
    "as_built_routes": _spec(
        "as_built_routes",
        (
            "id",
            "project_id",
            "status",
            "route_geom",
            "actual_length_meters",
            "submitted_at",
            "submitted_by_person_id",
            "reviewed_at",
            "reviewed_by_person_id",
            "review_notes",
            "fiber_segment_id",
            "report_file_path",
            "report_file_name",
            "report_generated_at",
            "variation_type",
            "variation_reason",
            "version",
            "work_order_ref",
            "erp_sync_status",
            "erp_reference",
            "erp_sync_at",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {
                "project_id",
                "submitted_by_person_id",
                "reviewed_by_person_id",
                "fiber_segment_id",
            }
        ),
        geom_columns=frozenset({"route_geom"}),
    ),
    "as_built_line_items": _spec(
        "as_built_line_items",
        (
            "id",
            "as_built_id",
            "item_type",
            "description",
            "cable_type",
            "fiber_count",
            "splice_count",
            "quantity",
            "unit_price",
            "amount",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"as_built_id"}),
    ),
    "proposed_route_revisions": _spec(
        "proposed_route_revisions",
        (
            "id",
            "quote_id",
            "revision_number",
            "status",
            "route_geom",
            "length_meters",
            "submitted_at",
            "submitted_by_person_id",
            "reviewed_at",
            "reviewed_by_person_id",
            "review_notes",
            "fiber_segment_id",
            "created_at",
        ),
        uuid_columns=frozenset(
            {"quote_id", "submitted_by_person_id", "reviewed_by_person_id", "fiber_segment_id"}
        ),
        geom_columns=frozenset({"route_geom"}),
    ),
    "installation_project_notes": _spec(
        "installation_project_notes",
        (
            "id",
            "project_id",
            "author_person_id",
            "body",
            "is_internal",
            "attachments",
            "created_at",
        ),
        uuid_columns=frozenset({"project_id", "author_person_id"}),
        json_columns=frozenset({"attachments"}),
    ),
}


# ---------------------------------------------------------------------------
# Stats / run context
# ---------------------------------------------------------------------------


@dataclass
class ImportStats:
    steps: dict[str, dict[str, int]] = field(default_factory=dict)
    watermarks: dict[str, str | None] = field(default_factory=dict)
    blockers: list[dict[str, Any]] = field(default_factory=list)

    def bump(self, step: str, key: str, amount: int = 1) -> None:
        self.steps.setdefault(step, {})
        self.steps[step][key] = self.steps[step].get(key, 0) + amount

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "watermarks": self.watermarks,
            "blockers": self.blockers,
        }


@dataclass
class DeferredLink:
    table: str
    row_id: str
    column: str
    target_table: str
    target_id: str


@dataclass
class RunContext:
    apply: bool
    state_file: str | None
    overlap_seconds: int
    subscriber_map: dict[str, str]
    staff_map: dict[str, str]
    # sub id sets for nullable/native FK resolution.
    project_ids: set[str]
    buildout_project_ids: set[str]
    fiber_segment_ids: set[str]
    stats: ImportStats = field(default_factory=ImportStats)
    reports: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {name: [] for name in REPORT_ACTIONS}
    )
    present_ids: dict[str, set[str]] = field(default_factory=dict)
    deferred: list[DeferredLink] = field(default_factory=list)
    staff_seen: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def since(self, table: str) -> datetime | None:
        return _state_watermark(
            self.state_file, watermark_key(table), self.overlap_seconds
        )

    def note_watermark(
        self, table: str, rows: list[dict[str, Any]], column: str
    ) -> None:
        latest: datetime | None = None
        for row in rows:
            parsed = _parse_datetime(row.get(column))
            if parsed and (latest is None or parsed > latest):
                latest = parsed
        self.stats.watermarks[watermark_key(table)] = _format_datetime(latest)

    def block(self, step: str, row: dict[str, Any]) -> None:
        self.stats.bump(step, "blocked")
        self.stats.blockers.append({"step": step, **row})
        self.reports["blockers"].append({"step": step, **row})

    def note_staff(self, table: str, column: str, value: Any) -> None:
        person_id = _uuid_or_none(value)
        if person_id:
            self.staff_seen.setdefault((table, column), set()).add(person_id.lower())


def _execute_upserts(
    sub: Connection,
    ctx: RunContext,
    step: str,
    spec: TableSpec,
    payloads: list[dict[str, Any]],
    existing_ids: set[str],
) -> None:
    sql = build_upsert_sql(spec)
    for payload in payloads:
        row_id = str(payload.get("id") or "").lower()
        ctx.stats.bump(step, "updated" if row_id in existing_ids else "created")
        if ctx.apply:
            sub.execute(text(sql), payload)
    ctx.present_ids.setdefault(spec.table, set()).update(
        str(payload["id"]).lower() for payload in payloads if payload.get("id")
    )


def _mark_present(ctx: RunContext, table: str, existing_ids: set[str]) -> None:
    ctx.present_ids.setdefault(table, set()).update(existing_ids)


def _fetch(
    crm: Connection,
    sql: str,
    *,
    since: datetime | None,
    watermark_column: str | None,
    params: dict[str, Any] | None = None,
    extra_where: str | None = None,
) -> list[dict[str, Any]]:
    clauses = []
    query_params = dict(params or {})
    if extra_where:
        clauses.append(extra_where)
    if since is not None and watermark_column:
        clauses.append(f"{watermark_column} >= :_since")
        query_params["_since"] = since
    if clauses:
        sql += "\nWHERE " + " AND ".join(clauses)
    if watermark_column:
        sql += f"\nORDER BY {watermark_column}, 1"
    return _rows(crm, sql, query_params)


# ---------------------------------------------------------------------------
# Pure resolution logic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeRef:
    value: str | None
    action: str  # "resolved" | "null" | "block"
    reason: str | None = None


def resolve_project_id(
    crm_project_id: str | None, project_ids: set[str]
) -> NativeRef:
    """installation_projects.project_id -> native ``projects`` (NOT NULL FK).

    Phase 3 carried CRM project UUIDs verbatim, so this is a direct membership
    check. A miss cannot be inserted (NOT NULL FK) and blocks the run.
    """
    key = _uuid_or_none(crm_project_id)
    if not key:
        return NativeRef(None, "block", "missing_project_id")
    if key.lower() in project_ids:
        return NativeRef(key, "resolved")
    return NativeRef(None, "block", "project_absent_in_sub")


def resolve_nullable_native(
    crm_id: str | None, present: set[str]
) -> NativeRef:
    """buildout_project_id / fiber_segment_id: keep if native, else NULL."""
    key = _uuid_or_none(crm_id)
    if not key:
        return NativeRef(None, "null")
    if key.lower() in present:
        return NativeRef(key, "resolved")
    return NativeRef(None, "null", "absent_in_sub")


def resolve_subscriber(
    crm_subscriber_id: str | None, subscriber_map: dict[str, str]
) -> NativeRef:
    """installation_projects.subscriber_id via link key 1 (nullable)."""
    key = _uuid_or_none(crm_subscriber_id)
    if not key:
        return NativeRef(None, "null")
    mapped = subscriber_map.get(key)
    if mapped:
        return NativeRef(mapped, "resolved")
    return NativeRef(None, "null", "unmapped_crm_subscriber")


# ---------------------------------------------------------------------------
# Steps (§B order)
# ---------------------------------------------------------------------------


def _import_vendors(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "vendors"
    rows = _fetch(
        crm,
        """
        SELECT id::text, name, code, contact_name, contact_email, contact_phone,
               license_number, service_area, is_active, notes, erp_id,
               created_at, updated_at
        FROM vendors
        """,
        since=ctx.since("vendors"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("vendors", rows, "updated_at")
    existing = _load_existing_ids(sub, "vendors")
    _mark_present(ctx, "vendors", existing)
    payloads = [
        {
            "id": row["id"],
            "name": row["name"],
            "code": row.get("code"),
            "contact_name": row.get("contact_name"),
            "contact_email": row.get("contact_email"),
            "contact_phone": row.get("contact_phone"),
            "license_number": row.get("license_number"),
            "service_area": row.get("service_area"),
            "is_active": bool(row.get("is_active")),
            "notes": row.get("notes"),
            "erp_id": row.get("erp_id"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    _execute_upserts(sub, ctx, step, TABLE_SPECS["vendors"], payloads, existing)


def _import_vendor_users(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "vendor_users"
    rows = _fetch(
        crm,
        """
        SELECT id::text, vendor_id::text, person_id::text, role, is_active,
               created_at, updated_at
        FROM vendor_users
        """,
        since=ctx.since("vendor_users"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("vendor_users", rows, "updated_at")
    existing = _load_existing_ids(sub, "vendor_users")
    vendors_present = ctx.present_ids.get("vendors", set())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["vendor_id"]).lower() not in vendors_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {"table": "vendor_users", "crm_id": row["id"], "parent": "vendor_id"}
            )
            continue
        ctx.note_staff("vendor_users", "person_id", row.get("person_id"))
        payloads.append(
            {
                "id": row["id"],
                "vendor_id": row["vendor_id"],
                "person_id": row["person_id"],
                "role": row.get("role"),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS["vendor_users"], payloads, existing)


def _import_installation_projects(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "installation_projects"
    rows = _fetch(
        crm,
        """
        SELECT id::text, project_id::text, buildout_project_id::text,
               subscriber_id::text, address_id::text, assigned_vendor_id::text,
               assignment_type::text, status::text, bidding_open_at,
               bidding_close_at, erp_purchase_order_id, approved_quote_id::text,
               created_by_person_id::text, notes, is_active,
               created_at, updated_at
        FROM installation_projects
        """,
        since=ctx.since("installation_projects"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("installation_projects", rows, "updated_at")
    existing = _load_existing_ids(sub, "installation_projects")
    _mark_present(ctx, "installation_projects", existing)
    vendors_present = ctx.present_ids.get("vendors", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        project_ref = resolve_project_id(row.get("project_id"), ctx.project_ids)
        if project_ref.action == "block":
            ctx.block(
                step,
                {
                    "crm_id": row["id"],
                    "crm_project_id": row.get("project_id"),
                    "status": row.get("status"),
                    "reason": project_ref.reason,
                },
            )
            ctx.reports["missing_project"].append(
                {
                    "crm_id": row["id"],
                    "crm_project_id": row.get("project_id"),
                    "reason": project_ref.reason,
                }
            )
            continue
        subscriber_ref = resolve_subscriber(
            row.get("subscriber_id"), ctx.subscriber_map
        )
        if subscriber_ref.reason:
            ctx.stats.bump(step, "unresolved_subscriber")
            ctx.reports["unresolved_subscribers"].append(
                {
                    "table": "installation_projects",
                    "crm_id": row["id"],
                    "crm_subscriber_id": row.get("subscriber_id"),
                    "reason": subscriber_ref.reason,
                }
            )
        buildout_ref = resolve_nullable_native(
            row.get("buildout_project_id"), ctx.buildout_project_ids
        )
        if buildout_ref.reason:
            ctx.stats.bump(step, "dangling_buildout_project")
            ctx.reports["dangling_buildout_project"].append(
                {
                    "crm_id": row["id"],
                    "crm_buildout_project_id": row.get("buildout_project_id"),
                }
            )
        assigned_vendor_id = _uuid_or_none(row.get("assigned_vendor_id"))
        if assigned_vendor_id and assigned_vendor_id.lower() not in vendors_present:
            ctx.stats.bump(step, "dangling_vendor")
            assigned_vendor_id = None
        ctx.note_staff(
            "installation_projects", "created_by_person_id",
            row.get("created_by_person_id"),
        )
        # approved_quote_id deferred to the finalize pass.
        if _uuid_or_none(row.get("approved_quote_id")):
            ctx.deferred.append(
                DeferredLink(
                    table="installation_projects",
                    row_id=str(row["id"]),
                    column="approved_quote_id",
                    target_table="project_quotes",
                    target_id=str(row["approved_quote_id"]),
                )
            )
        payloads.append(
            {
                "id": row["id"],
                "project_id": project_ref.value,
                "buildout_project_id": buildout_ref.value,
                "subscriber_id": subscriber_ref.value,
                "address_id": _uuid_or_none(row.get("address_id")),
                "assigned_vendor_id": assigned_vendor_id,
                "assignment_type": row.get("assignment_type"),
                "status": row.get("status") or "draft",
                "bidding_open_at": row.get("bidding_open_at"),
                "bidding_close_at": row.get("bidding_close_at"),
                "erp_purchase_order_id": row.get("erp_purchase_order_id"),
                "created_by_person_id": _uuid_or_none(row.get("created_by_person_id")),
                "notes": row.get("notes"),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["installation_projects"], payloads, existing
    )


def _import_project_quotes(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "project_quotes"
    rows = _fetch(
        crm,
        """
        SELECT id::text, project_id::text, vendor_id::text, status::text,
               currency, subtotal, vat_rate_percent, tax_total, total,
               valid_from, valid_until, submitted_at, reviewed_at,
               reviewed_by_person_id::text, review_notes,
               created_by_person_id::text, is_active, created_at, updated_at
        FROM project_quotes
        """,
        since=ctx.since("project_quotes"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("project_quotes", rows, "updated_at")
    existing = _load_existing_ids(sub, "project_quotes")
    _mark_present(ctx, "project_quotes", existing)
    projects_present = ctx.present_ids.get("installation_projects", set())
    vendors_present = ctx.present_ids.get("vendors", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["project_id"]).lower() not in projects_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {"table": "project_quotes", "crm_id": row["id"], "parent": "project_id"}
            )
            continue
        if str(row["vendor_id"]).lower() not in vendors_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {"table": "project_quotes", "crm_id": row["id"], "parent": "vendor_id"}
            )
            continue
        for column in ("reviewed_by_person_id", "created_by_person_id"):
            ctx.note_staff("project_quotes", column, row.get(column))
        payloads.append(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "vendor_id": row["vendor_id"],
                "status": row.get("status") or "draft",
                "currency": row.get("currency") or "NGN",
                "subtotal": row.get("subtotal"),
                "vat_rate_percent": row.get("vat_rate_percent"),
                "tax_total": row.get("tax_total"),
                "total": row.get("total"),
                "valid_from": row.get("valid_from"),
                "valid_until": row.get("valid_until"),
                "submitted_at": row.get("submitted_at"),
                "reviewed_at": row.get("reviewed_at"),
                "reviewed_by_person_id": _uuid_or_none(row.get("reviewed_by_person_id")),
                "review_notes": row.get("review_notes"),
                "created_by_person_id": _uuid_or_none(row.get("created_by_person_id")),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["project_quotes"], payloads, existing
    )


def _import_project_quote_line_items(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    """CRM ``quote_line_items`` -> sub ``project_quote_line_items`` (renamed to
    avoid the sales ``quote_line_items`` clash)."""
    step = "project_quote_line_items"
    rows = _fetch(
        crm,
        """
        SELECT id::text, quote_id::text, item_type, description, cable_type,
               fiber_count, splice_count, quantity, unit_price, amount, notes,
               client_ref::text, is_active, created_at, updated_at
        FROM quote_line_items
        """,
        since=ctx.since("quote_line_items"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("quote_line_items", rows, "updated_at")
    existing = _load_existing_ids(sub, "project_quote_line_items")
    quotes_present = ctx.present_ids.get("project_quotes", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["quote_id"]).lower() not in quotes_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {
                    "table": "project_quote_line_items",
                    "crm_id": row["id"],
                    "parent": "quote_id",
                }
            )
            continue
        payloads.append(
            {
                "id": row["id"],
                "quote_id": row["quote_id"],
                "item_type": row.get("item_type"),
                "description": row.get("description"),
                "cable_type": row.get("cable_type"),
                "fiber_count": row.get("fiber_count"),
                "splice_count": row.get("splice_count"),
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
                "amount": row.get("amount"),
                "notes": row.get("notes"),
                "client_ref": _uuid_or_none(row.get("client_ref")),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["project_quote_line_items"], payloads, existing
    )


def _import_as_built_routes(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "as_built_routes"
    rows = _fetch(
        crm,
        f"""
        SELECT id::text, project_id::text, proposed_revision_id::text,
               status::text, {geom_read_expr('route_geom')}, actual_length_meters,
               submitted_at, submitted_by_person_id::text, reviewed_at,
               reviewed_by_person_id::text, review_notes, fiber_segment_id::text,
               report_file_path, report_file_name, report_generated_at,
               variation_type::text, variation_reason, version, work_order_ref,
               erp_sync_status, erp_reference, erp_sync_at, created_at, updated_at
        FROM as_built_routes
        """,
        since=ctx.since("as_built_routes"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("as_built_routes", rows, "updated_at")
    existing = _load_existing_ids(sub, "as_built_routes")
    _mark_present(ctx, "as_built_routes", existing)
    projects_present = ctx.present_ids.get("installation_projects", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["project_id"]).lower() not in projects_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {
                    "table": "as_built_routes",
                    "crm_id": row["id"],
                    "parent": "project_id",
                }
            )
            continue
        fiber_ref = resolve_nullable_native(
            row.get("fiber_segment_id"), ctx.fiber_segment_ids
        )
        if fiber_ref.reason:
            ctx.stats.bump(step, "dangling_fiber_segment")
            ctx.reports["dangling_fiber_segment"].append(
                {
                    "table": "as_built_routes",
                    "crm_id": row["id"],
                    "crm_fiber_segment_id": row.get("fiber_segment_id"),
                }
            )
        for column in ("submitted_by_person_id", "reviewed_by_person_id"):
            ctx.note_staff("as_built_routes", column, row.get(column))
        # proposed_revision_id deferred to the finalize pass.
        if _uuid_or_none(row.get("proposed_revision_id")):
            ctx.deferred.append(
                DeferredLink(
                    table="as_built_routes",
                    row_id=str(row["id"]),
                    column="proposed_revision_id",
                    target_table="proposed_route_revisions",
                    target_id=str(row["proposed_revision_id"]),
                )
            )
        payloads.append(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "status": row.get("status") or "submitted",
                "route_geom": row.get("route_geom"),
                "actual_length_meters": row.get("actual_length_meters"),
                "submitted_at": row.get("submitted_at"),
                "submitted_by_person_id": _uuid_or_none(row.get("submitted_by_person_id")),
                "reviewed_at": row.get("reviewed_at"),
                "reviewed_by_person_id": _uuid_or_none(row.get("reviewed_by_person_id")),
                "review_notes": row.get("review_notes"),
                "fiber_segment_id": fiber_ref.value,
                "report_file_path": row.get("report_file_path"),
                "report_file_name": row.get("report_file_name"),
                "report_generated_at": row.get("report_generated_at"),
                "variation_type": row.get("variation_type"),
                "variation_reason": row.get("variation_reason"),
                "version": row.get("version") or 1,
                "work_order_ref": row.get("work_order_ref"),
                "erp_sync_status": row.get("erp_sync_status"),
                "erp_reference": row.get("erp_reference"),
                "erp_sync_at": row.get("erp_sync_at"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["as_built_routes"], payloads, existing
    )


def _import_as_built_line_items(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "as_built_line_items"
    rows = _fetch(
        crm,
        """
        SELECT id::text, as_built_id::text, item_type, description, cable_type,
               fiber_count, splice_count, quantity, unit_price, amount, notes,
               is_active, created_at, updated_at
        FROM as_built_line_items
        """,
        since=ctx.since("as_built_line_items"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("as_built_line_items", rows, "updated_at")
    existing = _load_existing_ids(sub, "as_built_line_items")
    routes_present = ctx.present_ids.get("as_built_routes", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["as_built_id"]).lower() not in routes_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {
                    "table": "as_built_line_items",
                    "crm_id": row["id"],
                    "parent": "as_built_id",
                }
            )
            continue
        payloads.append(
            {
                "id": row["id"],
                "as_built_id": row["as_built_id"],
                "item_type": row.get("item_type"),
                "description": row.get("description"),
                "cable_type": row.get("cable_type"),
                "fiber_count": row.get("fiber_count"),
                "splice_count": row.get("splice_count"),
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
                "amount": row.get("amount"),
                "notes": row.get("notes"),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["as_built_line_items"], payloads, existing
    )


def _import_proposed_route_revisions(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "proposed_route_revisions"
    rows = _fetch(
        crm,
        f"""
        SELECT id::text, quote_id::text, revision_number, status::text,
               {geom_read_expr('route_geom')}, length_meters, submitted_at,
               submitted_by_person_id::text, reviewed_at,
               reviewed_by_person_id::text, review_notes, fiber_segment_id::text,
               created_at
        FROM proposed_route_revisions
        """,
        since=ctx.since("proposed_route_revisions"),
        watermark_column="created_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("proposed_route_revisions", rows, "created_at")
    existing = _load_existing_ids(sub, "proposed_route_revisions")
    _mark_present(ctx, "proposed_route_revisions", existing)
    quotes_present = ctx.present_ids.get("project_quotes", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["quote_id"]).lower() not in quotes_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {
                    "table": "proposed_route_revisions",
                    "crm_id": row["id"],
                    "parent": "quote_id",
                }
            )
            continue
        fiber_ref = resolve_nullable_native(
            row.get("fiber_segment_id"), ctx.fiber_segment_ids
        )
        if fiber_ref.reason:
            ctx.stats.bump(step, "dangling_fiber_segment")
            ctx.reports["dangling_fiber_segment"].append(
                {
                    "table": "proposed_route_revisions",
                    "crm_id": row["id"],
                    "crm_fiber_segment_id": row.get("fiber_segment_id"),
                }
            )
        for column in ("submitted_by_person_id", "reviewed_by_person_id"):
            ctx.note_staff("proposed_route_revisions", column, row.get(column))
        payloads.append(
            {
                "id": row["id"],
                "quote_id": row["quote_id"],
                "revision_number": row.get("revision_number"),
                "status": row.get("status") or "draft",
                "route_geom": row.get("route_geom"),
                "length_meters": row.get("length_meters"),
                "submitted_at": row.get("submitted_at"),
                "submitted_by_person_id": _uuid_or_none(row.get("submitted_by_person_id")),
                "reviewed_at": row.get("reviewed_at"),
                "reviewed_by_person_id": _uuid_or_none(row.get("reviewed_by_person_id")),
                "review_notes": row.get("review_notes"),
                "fiber_segment_id": fiber_ref.value,
                "created_at": row["created_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["proposed_route_revisions"], payloads, existing
    )


def _import_installation_project_notes(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "installation_project_notes"
    rows = _fetch(
        crm,
        """
        SELECT id::text, project_id::text, author_person_id::text, body,
               is_internal, attachments::text, created_at
        FROM installation_project_notes
        """,
        since=ctx.since("installation_project_notes"),
        watermark_column="created_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("installation_project_notes", rows, "created_at")
    existing = _load_existing_ids(sub, "installation_project_notes")
    projects_present = ctx.present_ids.get("installation_projects", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["project_id"]).lower() not in projects_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            ctx.reports["skipped_unimported_parent"].append(
                {
                    "table": "installation_project_notes",
                    "crm_id": row["id"],
                    "parent": "project_id",
                }
            )
            continue
        ctx.note_staff(
            "installation_project_notes", "author_person_id",
            row.get("author_person_id"),
        )
        payloads.append(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "author_person_id": _uuid_or_none(row.get("author_person_id")),
                "body": row.get("body") or "",
                "is_internal": bool(row.get("is_internal")),
                "attachments": _verbatim_json(row.get("attachments"), None),
                "created_at": row["created_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["installation_project_notes"], payloads, existing
    )


def _apply_deferred_links(sub: Connection, ctx: RunContext) -> None:
    """Finalize pass for the two forward/mutual FKs the table order cannot
    satisfy inline (approved_quote_id, proposed_revision_id)."""
    step = "deferred_links"
    for link in ctx.deferred:
        target_present = ctx.present_ids.get(link.target_table, set())
        if link.target_id.lower() not in target_present:
            ctx.stats.bump(step, "unresolved_target")
            ctx.reports["deferred_link_unresolved"].append(
                {
                    "table": link.table,
                    "row_id": link.row_id,
                    "column": link.column,
                    "target_table": link.target_table,
                    "target_id": link.target_id,
                }
            )
            continue
        ctx.stats.bump(step, "linked")
        if ctx.apply:
            sub.execute(
                text(
                    f"""
                    UPDATE {link.table}
                    SET {link.column} = CAST(:target_id AS uuid)
                    WHERE id = CAST(:row_id AS uuid)
                      AND {link.column} IS DISTINCT FROM CAST(:target_id AS uuid)
                    """  # noqa: S608 — table/column from a fixed allow-list above
                ),
                {"row_id": link.row_id, "target_id": link.target_id},
            )


def _report_unmapped_staff(ctx: RunContext) -> None:
    for (table, column), person_ids in sorted(ctx.staff_seen.items()):
        for person_id in sorted(person_ids):
            if person_id not in ctx.staff_map:
                ctx.reports["unmapped_staff"].append(
                    {"table": table, "column": column, "crm_person_id": person_id}
                )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_import(*, sub: Connection, crm: Connection, ctx: RunContext) -> ImportStats:
    _import_vendors(sub, crm, ctx)
    _import_vendor_users(sub, crm, ctx)
    _import_installation_projects(sub, crm, ctx)
    _import_project_quotes(sub, crm, ctx)
    _import_project_quote_line_items(sub, crm, ctx)
    _import_as_built_routes(sub, crm, ctx)
    _import_as_built_line_items(sub, crm, ctx)
    _import_proposed_route_revisions(sub, crm, ctx)
    _import_installation_project_notes(sub, crm, ctx)
    _apply_deferred_links(sub, ctx)
    _report_unmapped_staff(ctx)
    return ctx.stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--staff-map",
        help=(
            "staff_map.csv from build_crm_staff_map.py; person UUIDs carry "
            "verbatim either way — the map only feeds the unmapped_staff CSV."
        ),
    )
    parser.add_argument(
        "--state-file",
        help="JSON state file with per-CRM-table watermarks for incremental runs.",
    )
    parser.add_argument(
        "--state-overlap-seconds",
        type=int,
        default=600,
        help="Subtract this overlap from every state-file watermark.",
    )
    parser.add_argument(
        "--out",
        default="vendor-routes-import-report",
        help="Directory for the summary JSON and per-action CSVs.",
    )
    args = parser.parse_args()

    out = Path(args.out)
    staff_map = _load_staff_map(args.staff_map)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        sub_trans = sub.begin()
        crm.execute(text("SET TRANSACTION READ ONLY"))
        if not args.apply:
            sub.execute(text("SET TRANSACTION READ ONLY"))
        try:
            ctx = RunContext(
                apply=args.apply,
                state_file=args.state_file,
                overlap_seconds=args.state_overlap_seconds,
                subscriber_map=_load_subscriber_map(sub),
                staff_map=staff_map,
                project_ids=_load_existing_ids(sub, "projects"),
                buildout_project_ids=_load_existing_ids(sub, "buildout_projects"),
                fiber_segment_ids=_load_existing_ids(sub, "fiber_segments"),
            )
            stats = run_import(sub=sub, crm=crm, ctx=ctx)
        except Exception:
            sub_trans.rollback()
            crm.rollback()
            raise
        if args.apply and not stats.blockers:
            sub_trans.commit()
            write_state_keys(args.state_file, stats.watermarks)
        else:
            sub_trans.rollback()
        crm.rollback()

    for name in REPORT_ACTIONS:
        _write_csv(out / f"{name}.csv", ctx.reports[name])

    report = {
        "apply": args.apply,
        "staff_map": args.staff_map,
        "staff_map_entries": len(staff_map),
        "state_file": args.state_file,
        "state_overlap_seconds": args.state_overlap_seconds,
        "output_dir": str(out),
        "stats": stats.as_dict(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))
    if stats.blockers:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
