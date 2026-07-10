#!/usr/bin/env python3
"""Phase 3 CRM backfill: projects, leads/pipeline, quotes, sales orders,
referrals and work links into native sub tables (20-phase3-projects-sales.md
§3.5, PR 3 of §6).

CRM UUIDs become sub PKs for every Phase 3 table (§3.4), so all upserts are
idempotent ``ON CONFLICT`` on the CRM key itself — no marker metadata needed
for dedupe. Steps run in FK-driven order (§3.5; organizations/memberships and
the party backfill are PR 1's ``backfill_party_status.py`` and are assumed
done):

  1. ``pipelines`` -> ``pipeline_stages`` -> ``leads``
  2. ``support_tickets.lead_id`` backfill (Phase 1 carried the CRM values in
     the ticket fetch but never wrote the column) — the deferred
     ``NOT VALID`` FK + ``VALIDATE`` runs only with ``--validate-lead-fk``
  3. ``quotes`` -> ``quote_line_items``
  4. ``document_sequences['sales_order_number']`` seeded with the CRM row's
     ``next_value`` so ``SO-%06d`` numbering continues the CRM sequence
     (§1.5) -> ``sales_orders`` -> ``sales_order_lines`` ->
     ``subscribers.sales_order_id`` (link key 2)
  5. projects family: ``service_teams`` refresh (FK target) ->
     ``project_templates`` -> ``project_template_tasks`` ->
     ``project_template_task_dependency`` -> ``projects`` ->
     ``project_tasks`` (two-phase: rows first, ``parent_task_id`` re-linked
     second so self-FK ordering never matters) -> assignees -> dependencies
     -> both comment tables (provenance metadata per §1.2)
  6. ``referral_codes`` -> ``referrals`` -> ``work_links`` (only rows whose
     source and target types are Phase 3 entities; work_order/ticket-typed
     rows wait for Phase 2 and land in a deferred CSV)

Column re-pointing (§1.8):
  * customer-party ``person_id`` columns resolve to sub ``subscribers.id``
    through the ``--party-map`` artifact (``person_subscriber_map.csv`` from
    ``backfill_party_status.py``), merged over the same links already stamped
    in sub (``subscribers.metadata->>'crm_person_id'``);
  * ``projects.subscriber_id`` resolves through link key 1
    (``subscribers.crm_subscriber_id`` + ``crm_alias_ids``);
  * staff person / Phase 4 agent / campaign / Phase 5 inventory UUIDs carry
    verbatim (FK-dropped columns); ``--staff-map`` only feeds the
    informational unmapped-staff CSV;
  * ``project_tasks.ticket_id`` re-keys through the Phase 1 map
    (``support_tickets.metadata->>'crm_ticket_id'``); dangling ids null out
    into a CSV (risk #14);
  * ``referrals.referred_person_id`` + ``referred_subscriber_id`` collapse
    into one sub column, cross-checked; disagreements are reported (§3.6).

Blockers policy (matches ``import_crm_tickets_phase1.py``): an active row
whose NOT NULL subscriber link cannot be resolved blocks the whole run (no
commit, exit 2); inactive unresolved rows are skipped into a CSV. Partial
uniques (open lead per subscriber+pipeline, active referral per referred
subscriber) are pre-flighted on the planned rows — collapse collisions block.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Dry-run by default; ``--apply`` writes to sub (the CRM session is always
read-only). ``--state-file`` keeps one ``updated_at``/``created_at``
watermark per CRM table for incremental re-runs while the mirrors + webhooks
stay live (§3.5 tail); children of re-fetched parents are re-synced even when
their own watermark misses them.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.migration.import_crm_tickets_phase1 import (  # noqa: E402
    _engine_from_env,
    _format_datetime,
    _json,
    _load_staff_map,
    _load_subscriber_map,
    _parse_datetime,
    _read_state,
    _rows,
    _state_watermark,
    _upsert_service_teams,
    _uuid_or_none,
)

IMPORT_SOURCE = "dotmac_crm_phase3"
SALES_ORDER_SEQUENCE_KEY = "sales_order_number"
SALES_ORDER_NUMBER_PREFIX = "SO-"
LEAD_FK_NAME = "fk_support_tickets_lead_id"
UUID_SENTINEL = "00000000-0000-0000-0000-000000000000"

# Lead statuses outside the open-lead partial unique (§1.3).
LEAD_CLOSED_STATUSES = {"won", "lost"}

# work_links rows importable in Phase 3 (§3.5 step 7): both endpoints must be
# Phase 3 entities; work_order rows wait for the Phase 2 flip, ticket rows
# would need the Phase 1 re-key and ride along with them.
PHASE3_WORK_LINK_TYPES = frozenset({"project", "project_task", "lead", "sales_order"})

# §3.5 step order (also asserted by the tests). Parenthesised sub-steps of a
# vertical run inside one step function.
STEP_ORDER = (
    "pipelines",
    "pipeline_stages",
    "leads",
    "support_ticket_lead_ids",
    "quotes",
    "quote_line_items",
    "sales_order_sequence",
    "sales_orders",
    "sales_order_lines",
    "subscriber_sales_order_ids",
    "project_templates",
    "project_template_tasks",
    "project_template_task_dependency",
    "projects",
    "project_tasks",
    "project_task_assignees",
    "project_task_dependencies",
    "project_task_comments",
    "project_comments",
    "referral_codes",
    "referrals",
    "work_links",
)

REPORT_ACTIONS = (
    "blockers",
    "skipped_unresolved_inactive",
    "unresolved_sales_orders",
    "dangling_lead_refs",
    "dangling_ticket_refs",
    "referred_link_disagreements",
    "lead_open_unique_conflicts",
    "referral_referred_unique_conflicts",
    "deferred_work_links",
    "subscriber_sales_order_mismatch",
    "unmapped_staff",
)

# (table, column) pairs whose UUIDs are staff people carried verbatim (§1.8);
# used only for the informational unmapped-staff report.
STAFF_UUID_COLUMNS = (
    ("projects", "created_by_person_id"),
    ("projects", "owner_person_id"),
    ("projects", "manager_person_id"),
    ("projects", "project_manager_person_id"),
    ("projects", "assistant_manager_person_id"),
    ("project_tasks", "assigned_to_person_id"),
    ("project_tasks", "created_by_person_id"),
    ("project_task_comments", "author_person_id"),
    ("project_comments", "author_person_id"),
    ("work_links", "created_by_person_id"),
    ("quotes", "owner_person_id"),
)


# ---------------------------------------------------------------------------
# State-file watermarks (one key per CRM table, phase1 semantics).
# ---------------------------------------------------------------------------


def watermark_key(table: str) -> str:
    return f"last_crm_{table}_max_ts"


def write_state_keys(path: str | None, values: dict[str, str | None]) -> None:
    """Merge-write watermarks; None values preserve existing keys."""
    updates = {key: value for key, value in values.items() if value}
    if not path or not updates:
        return
    payload = _read_state(path)
    payload.update(updates)
    payload["updated_at"] = datetime.now(UTC).isoformat()
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Input artifacts
# ---------------------------------------------------------------------------


def _load_party_map_csv(path: str | None) -> dict[str, str]:
    """``crm_person_id -> subscriber_id`` from person_subscriber_map.csv."""
    if not path:
        return {}
    mapping: dict[str, str] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            person_id = str(row.get("crm_person_id") or "").strip().lower()
            subscriber_id = str(row.get("subscriber_id") or "").strip().lower()
            if not person_id or not subscriber_id:
                continue
            existing = mapping.get(person_id)
            if existing and existing != subscriber_id:
                raise SystemExit(
                    f"Conflicting party-map rows for crm_person_id {person_id}"
                )
            mapping[person_id] = subscriber_id
    return mapping


def _load_party_map_from_sub(sub: Connection) -> dict[str, str]:
    """Link key 4 fallback: subscribers.metadata->>'crm_person_id' in sub.

    The party backfill stamps this on every row it resolves or creates, so a
    fresh DB read reproduces the CSV artifact. CSV entries win on conflict
    (the reviewed artifact is authoritative).
    """
    mapping: dict[str, str] = {}
    for row in _rows(
        sub,
        """
        SELECT id::text AS subscriber_id,
               lower(metadata->>'crm_person_id') AS crm_person_id
        FROM subscribers
        WHERE metadata->>'crm_person_id' IS NOT NULL
        ORDER BY created_at, id
        """,
    ):
        person_id = str(row["crm_person_id"])
        mapping.setdefault(person_id, str(row["subscriber_id"]).lower())
    return mapping


def merge_party_maps(
    csv_map: dict[str, str], sub_map: dict[str, str]
) -> dict[str, str]:
    merged = dict(sub_map)
    merged.update(csv_map)
    return merged


def _load_ticket_rekey_map(sub: Connection) -> dict[str, str]:
    """Phase 1 crm_ticket_id -> final sub support_ticket id map (§3.4)."""
    return {
        str(row["crm_ticket_id"]).lower(): str(row["support_ticket_id"])
        for row in _rows(
            sub,
            """
            SELECT id::text AS support_ticket_id,
                   lower(metadata->>'crm_ticket_id') AS crm_ticket_id
            FROM support_tickets
            WHERE metadata->>'crm_ticket_id' IS NOT NULL
            """,
        )
    }


def _load_existing_ids(sub: Connection, table: str) -> set[str]:
    return {
        str(row["id"]).lower()
        for row in _rows(sub, f"SELECT id::text AS id FROM {table}")  # noqa: S608
    }


# ---------------------------------------------------------------------------
# Pure resolution / planning logic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartyResolution:
    subscriber_id: str | None
    action: str  # "resolved" | "skip" | "block"
    reason: str | None = None


def resolve_party_subscriber(
    person_id: str | None,
    *,
    is_active: bool,
    party_map: dict[str, str],
) -> PartyResolution:
    """Resolve a NOT NULL customer-party link (§3.2/§1.8).

    Active rows with an unresolved person block the run — the party backfill
    covers every person referenced by an active row, so a miss means it is
    stale. Inactive history is skipped into a CSV instead (the NOT NULL FK
    leaves no unlinked-import option, unlike Phase 1 tickets).
    """
    key = (person_id or "").strip().lower()
    if not key:
        return PartyResolution(None, "block", "missing_person_id")
    subscriber_id = party_map.get(key)
    if subscriber_id:
        return PartyResolution(subscriber_id, "resolved")
    if is_active:
        return PartyResolution(None, "block", "unresolved_person")
    return PartyResolution(None, "skip", "unresolved_person_inactive")


def resolve_sales_order_subscriber(
    *,
    person_id: str | None,
    quote_person_id: str | None,
    is_active: bool,
    party_map: dict[str, str],
) -> tuple[PartyResolution, str | None]:
    """§3.5 step 5: SO person via the party map, falling back to the quote's
    person (quote.subscriber post-import). Returns (resolution, method)."""
    primary = resolve_party_subscriber(
        person_id, is_active=is_active, party_map=party_map
    )
    if primary.subscriber_id:
        return primary, "person"
    fallback = resolve_party_subscriber(
        quote_person_id, is_active=is_active, party_map=party_map
    )
    if fallback.subscriber_id:
        return fallback, "quote_person"
    return primary, None


def resolve_referred_subscriber(
    *,
    referred_person_id: str | None,
    crm_referred_subscriber_id: str | None,
    party_map: dict[str, str],
    subscriber_map: dict[str, str],
) -> tuple[str | None, dict[str, str] | None]:
    """Collapse CRM ``referred_person_id`` + ``referred_subscriber_id`` into
    the single sub column (§1.6), cross-checking both link paths (§3.6).

    The subscriber path (link key 1) wins when both resolve — it is the
    direct customer link; a disagreement row is returned for the CSV.
    """
    via_person = None
    if referred_person_id:
        via_person = party_map.get(referred_person_id.strip().lower())
    via_subscriber = None
    if crm_referred_subscriber_id:
        via_subscriber = subscriber_map.get(str(crm_referred_subscriber_id))
        via_subscriber = via_subscriber.lower() if via_subscriber else None

    disagreement: dict[str, str] | None = None
    if via_person and via_subscriber and via_person != via_subscriber:
        disagreement = {
            "crm_referred_person_id": str(referred_person_id),
            "crm_referred_subscriber_id": str(crm_referred_subscriber_id),
            "via_person": via_person,
            "via_subscriber": via_subscriber,
        }
    return via_subscriber or via_person, disagreement


def rekey_ticket_id(
    crm_ticket_id: str | None, ticket_map: dict[str, str]
) -> tuple[str | None, bool]:
    """Map a CRM ticket UUID through the Phase 1 re-key map (§1.2).

    Returns ``(sub_ticket_id, dangling)`` — dangling ids null out (risk #14).
    """
    if not crm_ticket_id:
        return None, False
    mapped = ticket_map.get(crm_ticket_id.strip().lower())
    if mapped:
        return mapped, False
    return None, True


def plan_lead_open_unique_conflicts(
    leads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pre-flight ``uq_leads_one_open_per_subscriber_pipeline`` (§1.3).

    CRM's own index guarantees no per-person dupes; collisions here mean the
    person->subscriber collapse merged two open leads — a blocker.
    Rows need ``id``, ``subscriber_id``, ``pipeline_id``, ``status``,
    ``is_active`` keys (post-resolution).
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for lead in leads:
        if not lead.get("is_active"):
            continue
        if str(lead.get("status") or "") in LEAD_CLOSED_STATUSES:
            continue
        subscriber_id = str(lead.get("subscriber_id") or "")
        if not subscriber_id:
            continue
        key = (subscriber_id, str(lead.get("pipeline_id") or UUID_SENTINEL))
        groups.setdefault(key, []).append(str(lead["id"]))
    return [
        {
            "subscriber_id": subscriber_id,
            "pipeline_id": pipeline_id,
            "lead_ids": ";".join(sorted(lead_ids)),
        }
        for (subscriber_id, pipeline_id), lead_ids in sorted(groups.items())
        if len(lead_ids) > 1
    ]


def plan_referral_referred_unique_conflicts(
    referrals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pre-flight ``uq_referrals_active_referred_subscriber`` (§1.6)."""
    groups: dict[str, list[str]] = {}
    for referral in referrals:
        if not referral.get("is_active"):
            continue
        referred = referral.get("referred_subscriber_id")
        if not referred:
            continue
        groups.setdefault(str(referred), []).append(str(referral["id"]))
    return [
        {"referred_subscriber_id": referred, "referral_ids": ";".join(sorted(ids))}
        for referred, ids in sorted(groups.items())
        if len(ids) > 1
    ]


def work_link_is_phase3(source_type: str | None, target_type: str | None) -> bool:
    """§3.5 step 7: both endpoints must be Phase 3 entity types."""
    return (
        str(source_type or "") in PHASE3_WORK_LINK_TYPES
        and str(target_type or "") in PHASE3_WORK_LINK_TYPES
    )


def parse_order_number(order_number: str | None) -> int | None:
    """Numeric part of an ``SO-%06d`` order number, None for foreign shapes."""
    value = str(order_number or "").strip()
    if not value.startswith(SALES_ORDER_NUMBER_PREFIX):
        return None
    digits = value[len(SALES_ORDER_NUMBER_PREFIX) :]
    if not digits.isdigit():
        return None
    return int(digits)


def seed_sequence_next_value(
    *,
    sub_next_value: int | None,
    crm_next_value: int | None,
    max_order_number: int | None,
) -> int:
    """Continuation value for ``document_sequences['sales_order_number']``.

    Never decreases an existing sub value; always clears the highest imported
    ``SO-%06d`` so post-flip numbering cannot collide (risk #10).
    """
    return max(
        sub_next_value or 1,
        crm_next_value or 1,
        (max_order_number or 0) + 1,
    )


def provenance_metadata(
    raw_metadata: Any, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """CRM metadata dict + Phase 3 provenance keys (§3.2 house pattern)."""
    metadata = _json(raw_metadata, {}) or {}
    if not isinstance(metadata, dict):
        metadata = {"crm_metadata_raw": metadata}
    merged = dict(metadata)
    for key, value in (extra or {}).items():
        if value is not None:
            merged[key] = value
    merged["crm_import_source"] = IMPORT_SOURCE
    return merged


def _verbatim_json(raw: Any, default: Any) -> str | None:
    value = _json(raw, default)
    if value is None:
        return None
    return json.dumps(value)


# ---------------------------------------------------------------------------
# Generic upsert SQL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    table: str
    columns: tuple[str, ...]
    uuid_columns: frozenset[str] = frozenset()
    json_columns: frozenset[str] = frozenset()
    conflict_columns: tuple[str, ...] = ("id",)
    on_conflict: str = "update"  # "update" | "nothing"
    immutable_columns: tuple[str, ...] = ("id", "created_at")


def build_upsert_sql(spec: TableSpec) -> str:
    def _placeholder(column: str) -> str:
        if column in spec.uuid_columns:
            return f"CAST(:{column} AS uuid)"
        if column in spec.json_columns:
            return f"CAST(:{column} AS json)"
        return f":{column}"

    columns_sql = ", ".join(spec.columns)
    values_sql = ", ".join(_placeholder(column) for column in spec.columns)
    conflict_sql = ", ".join(spec.conflict_columns)
    sql = (
        f"INSERT INTO {spec.table} ({columns_sql})\n"
        f"VALUES ({values_sql})\n"
        f"ON CONFLICT ({conflict_sql}) "
    )
    if spec.on_conflict == "nothing":
        return sql + "DO NOTHING"
    updatable = [
        column
        for column in spec.columns
        if column not in spec.conflict_columns and column not in spec.immutable_columns
    ]
    update_sql = ",\n    ".join(f"{column} = EXCLUDED.{column}" for column in updatable)
    return sql + f"DO UPDATE SET\n    {update_sql}"


UUID_COLS_COMMON = frozenset({"id"})


def _spec(
    table: str,
    columns: tuple[str, ...],
    *,
    uuid_columns: frozenset[str] = frozenset(),
    json_columns: frozenset[str] = frozenset(),
    conflict_columns: tuple[str, ...] = ("id",),
    on_conflict: str = "update",
) -> TableSpec:
    return TableSpec(
        table=table,
        columns=columns,
        uuid_columns=UUID_COLS_COMMON | uuid_columns,
        json_columns=json_columns,
        conflict_columns=conflict_columns,
        on_conflict=on_conflict,
    )


TABLE_SPECS: dict[str, TableSpec] = {
    "pipelines": _spec(
        "pipelines",
        ("id", "name", "is_active", "metadata", "created_at", "updated_at"),
        json_columns=frozenset({"metadata"}),
    ),
    "pipeline_stages": _spec(
        "pipeline_stages",
        (
            "id",
            "pipeline_id",
            "name",
            "order_index",
            "is_active",
            "default_probability",
            "metadata",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"pipeline_id"}),
        json_columns=frozenset({"metadata"}),
    ),
    "leads": _spec(
        "leads",
        (
            "id",
            "subscriber_id",
            "pipeline_id",
            "stage_id",
            "owner_agent_id",
            "title",
            "status",
            "estimated_value",
            "currency",
            "probability",
            "expected_close_date",
            "closed_at",
            "lost_reason",
            "lead_source",
            "campaign_id",
            "campaign_recipient_id",
            "region",
            "address",
            "notes",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {
                "subscriber_id",
                "pipeline_id",
                "stage_id",
                "owner_agent_id",
                "campaign_id",
                "campaign_recipient_id",
            }
        ),
        json_columns=frozenset({"metadata"}),
    ),
    "quotes": _spec(
        "quotes",
        (
            "id",
            "subscriber_id",
            "lead_id",
            "owner_person_id",
            "status",
            "currency",
            "subtotal",
            "tax_rate",
            "tax_total",
            "total",
            "expires_at",
            "sent_at",
            "notes",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"subscriber_id", "lead_id", "owner_person_id"}),
        json_columns=frozenset({"metadata"}),
    ),
    "quote_line_items": _spec(
        "quote_line_items",
        (
            "id",
            "quote_id",
            "inventory_item_id",
            "description",
            "quantity",
            "unit_price",
            "discount_percent",
            "amount",
            "metadata",
            "created_at",
        ),
        uuid_columns=frozenset({"quote_id", "inventory_item_id"}),
        json_columns=frozenset({"metadata"}),
    ),
    "sales_orders": _spec(
        "sales_orders",
        (
            "id",
            "quote_id",
            "subscriber_id",
            "owner_agent_id",
            "source",
            "order_number",
            "status",
            "payment_status",
            "currency",
            "subtotal",
            "tax_total",
            "total",
            "amount_paid",
            "balance_due",
            "payment_due_date",
            "paid_at",
            "deposit_required",
            "deposit_paid",
            "contract_signed",
            "signed_at",
            "notes",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"quote_id", "subscriber_id", "owner_agent_id"}),
        json_columns=frozenset({"metadata"}),
    ),
    "sales_order_lines": _spec(
        "sales_order_lines",
        (
            "id",
            "sales_order_id",
            "inventory_item_id",
            "description",
            "quantity",
            "unit_price",
            "amount",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"sales_order_id", "inventory_item_id"}),
        json_columns=frozenset({"metadata"}),
    ),
    "project_templates": _spec(
        "project_templates",
        (
            "id",
            "name",
            "project_type",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        ),
    ),
    "project_template_tasks": _spec(
        "project_template_tasks",
        (
            "id",
            "template_id",
            "title",
            "description",
            "status",
            "priority",
            "sort_order",
            "effort_hours",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset({"template_id"}),
    ),
    "project_template_task_dependency": _spec(
        "project_template_task_dependency",
        (
            "id",
            "template_task_id",
            "depends_on_template_task_id",
            "dependency_type",
            "lag_days",
        ),
        uuid_columns=frozenset({"template_task_id", "depends_on_template_task_id"}),
    ),
    "projects": _spec(
        "projects",
        (
            "id",
            "name",
            "code",
            "number",
            "erpnext_id",
            "description",
            "customer_address",
            "project_type",
            "project_template_id",
            "status",
            "priority",
            "subscriber_id",
            "lead_id",
            "created_by_person_id",
            "owner_person_id",
            "manager_person_id",
            "project_manager_person_id",
            "assistant_manager_person_id",
            "service_team_id",
            "start_at",
            "due_at",
            "completed_at",
            "region",
            "tags",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {
                "project_template_id",
                "subscriber_id",
                "lead_id",
                "created_by_person_id",
                "owner_person_id",
                "manager_person_id",
                "project_manager_person_id",
                "assistant_manager_person_id",
                "service_team_id",
            }
        ),
        json_columns=frozenset({"tags", "metadata"}),
    ),
    # parent_task_id is deliberately absent: phase 2 of the two-phase task
    # apply re-links it after every row exists (self-FK ordering).
    "project_tasks": _spec(
        "project_tasks",
        (
            "id",
            "project_id",
            "title",
            "number",
            "erpnext_id",
            "description",
            "template_task_id",
            "status",
            "priority",
            "assigned_to_person_id",
            "created_by_person_id",
            "ticket_id",
            "work_order_id",
            "start_at",
            "due_at",
            "completed_at",
            "effort_hours",
            "tags",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {
                "project_id",
                "template_task_id",
                "assigned_to_person_id",
                "created_by_person_id",
                "ticket_id",
                "work_order_id",
            }
        ),
        json_columns=frozenset({"tags", "metadata"}),
    ),
    "project_task_assignees": _spec(
        "project_task_assignees",
        ("task_id", "person_id", "created_at"),
        uuid_columns=frozenset({"task_id", "person_id"}),
        conflict_columns=("task_id", "person_id"),
        on_conflict="nothing",
    ),
    "project_task_dependencies": _spec(
        "project_task_dependencies",
        ("id", "task_id", "depends_on_task_id", "dependency_type", "lag_days"),
        uuid_columns=frozenset({"task_id", "depends_on_task_id"}),
    ),
    "project_task_comments": _spec(
        "project_task_comments",
        (
            "id",
            "task_id",
            "author_person_id",
            "body",
            "attachments",
            "metadata",
            "created_at",
        ),
        uuid_columns=frozenset({"task_id", "author_person_id"}),
        json_columns=frozenset({"attachments", "metadata"}),
        on_conflict="nothing",
    ),
    "project_comments": _spec(
        "project_comments",
        (
            "id",
            "project_id",
            "author_person_id",
            "body",
            "attachments",
            "metadata",
            "created_at",
        ),
        uuid_columns=frozenset({"project_id", "author_person_id"}),
        json_columns=frozenset({"attachments", "metadata"}),
        on_conflict="nothing",
    ),
    "referral_codes": _spec(
        "referral_codes",
        ("id", "subscriber_id", "code", "is_active", "created_at"),
        uuid_columns=frozenset({"subscriber_id"}),
    ),
    "referrals": _spec(
        "referrals",
        (
            "id",
            "referrer_subscriber_id",
            "referral_code_id",
            "referred_subscriber_id",
            "referred_lead_id",
            "status",
            "reward_amount",
            "reward_currency",
            "reward_status",
            "reward_issued_at",
            "qualified_at",
            "source",
            "notes",
            "metadata",
            "is_active",
            "created_at",
            "updated_at",
        ),
        uuid_columns=frozenset(
            {
                "referrer_subscriber_id",
                "referral_code_id",
                "referred_subscriber_id",
                "referred_lead_id",
            }
        ),
        json_columns=frozenset({"metadata"}),
    ),
    "work_links": _spec(
        "work_links",
        (
            "id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "link_type",
            "contract_name",
            "created_by_person_id",
            "metadata",
            "created_at",
        ),
        uuid_columns=frozenset({"source_id", "target_id", "created_by_person_id"}),
        json_columns=frozenset({"metadata"}),
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
class RunContext:
    apply: bool
    state_file: str | None
    overlap_seconds: int
    party_map: dict[str, str]
    subscriber_map: dict[str, str]
    staff_map: dict[str, str]
    ticket_map: dict[str, str]
    stats: ImportStats = field(default_factory=ImportStats)
    reports: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {name: [] for name in REPORT_ACTIONS}
    )
    # Per-table ids present in sub after this run (existing + planned) so
    # downstream steps can validate nullable FK references in dry-run too.
    present_ids: dict[str, set[str]] = field(default_factory=dict)
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
    *,
    id_column: str = "id",
) -> None:
    sql = build_upsert_sql(spec)
    for payload in payloads:
        row_id = str(payload.get(id_column) or "").lower()
        existed = row_id in existing_ids
        ctx.stats.bump(step, "updated" if existed else "created")
        if ctx.apply:
            sub.execute(text(sql), payload)
    ctx.present_ids.setdefault(spec.table, set()).update(
        str(payload.get(id_column)).lower()
        for payload in payloads
        if payload.get(id_column)
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
# Step implementations (§3.5 order)
# ---------------------------------------------------------------------------


def _import_pipelines(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "pipelines"
    rows = _fetch(
        crm,
        """
        SELECT id::text, name, is_active, metadata::text,
               created_at, updated_at
        FROM crm_pipelines
        """,
        since=ctx.since("crm_pipelines"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_pipelines", rows, "updated_at")
    existing = _load_existing_ids(sub, "pipelines")
    _mark_present(ctx, "pipelines", existing)
    payloads = [
        {
            "id": row["id"],
            "name": row["name"],
            "is_active": bool(row["is_active"]),
            "metadata": _verbatim_json(row.get("metadata"), None),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    _execute_upserts(sub, ctx, step, TABLE_SPECS["pipelines"], payloads, existing)


def _import_pipeline_stages(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "pipeline_stages"
    rows = _fetch(
        crm,
        """
        SELECT id::text, pipeline_id::text, name, order_index, is_active,
               default_probability, metadata::text, created_at, updated_at
        FROM crm_pipeline_stages
        """,
        since=ctx.since("crm_pipeline_stages"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_pipeline_stages", rows, "updated_at")
    existing = _load_existing_ids(sub, "pipeline_stages")
    _mark_present(ctx, "pipeline_stages", existing)
    payloads = [
        {
            "id": row["id"],
            "pipeline_id": row["pipeline_id"],
            "name": row["name"],
            "order_index": row["order_index"],
            "is_active": bool(row["is_active"]),
            "default_probability": row["default_probability"],
            "metadata": _verbatim_json(row.get("metadata"), None),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    _execute_upserts(sub, ctx, step, TABLE_SPECS["pipeline_stages"], payloads, existing)


def _import_leads(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "leads"
    rows = _fetch(
        crm,
        """
        SELECT id::text, person_id::text, pipeline_id::text, stage_id::text,
               owner_agent_id::text, title, status::text, estimated_value,
               currency, probability, expected_close_date, closed_at,
               lost_reason, lead_source, campaign_id::text,
               campaign_recipient_id::text, region, address, notes,
               metadata::text, is_active, created_at, updated_at
        FROM crm_leads
        """,
        since=ctx.since("crm_leads"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_leads", rows, "updated_at")
    existing = _load_existing_ids(sub, "leads")
    _mark_present(ctx, "leads", existing)

    payloads: list[dict[str, Any]] = []
    for row in rows:
        resolution = resolve_party_subscriber(
            row.get("person_id"),
            is_active=bool(row.get("is_active")),
            party_map=ctx.party_map,
        )
        if resolution.action == "block":
            ctx.block(
                step,
                {
                    "crm_id": row["id"],
                    "crm_person_id": row.get("person_id"),
                    "status": row.get("status"),
                    "reason": resolution.reason,
                },
            )
            continue
        if resolution.action == "skip":
            ctx.stats.bump(step, "skipped")
            ctx.reports["skipped_unresolved_inactive"].append(
                {
                    "table": "leads",
                    "crm_id": row["id"],
                    "crm_person_id": row.get("person_id"),
                    "reason": resolution.reason,
                }
            )
            continue
        payloads.append(
            {
                "id": row["id"],
                "subscriber_id": resolution.subscriber_id,
                "pipeline_id": row.get("pipeline_id"),
                "stage_id": row.get("stage_id"),
                "owner_agent_id": row.get("owner_agent_id"),
                "title": row.get("title"),
                "status": row.get("status") or "new",
                "estimated_value": row.get("estimated_value"),
                "currency": row.get("currency"),
                "probability": row.get("probability"),
                "expected_close_date": row.get("expected_close_date"),
                "closed_at": row.get("closed_at"),
                "lost_reason": row.get("lost_reason"),
                "lead_source": row.get("lead_source"),
                "campaign_id": row.get("campaign_id"),
                "campaign_recipient_id": row.get("campaign_recipient_id"),
                "region": row.get("region"),
                "address": row.get("address"),
                "notes": row.get("notes"),
                "metadata": json.dumps(
                    provenance_metadata(
                        row.get("metadata"),
                        {"crm_person_id": row.get("person_id")},
                    )
                ),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    conflicts = plan_lead_open_unique_conflicts(payloads)
    for conflict in conflicts:
        ctx.reports["lead_open_unique_conflicts"].append(conflict)
        ctx.block(step, {"reason": "lead_open_unique_conflict", **conflict})
    if conflicts:
        return
    _execute_upserts(sub, ctx, step, TABLE_SPECS["leads"], payloads, existing)


def _backfill_support_ticket_lead_ids(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    """§3.5 step 3 tail: materialize ``support_tickets.lead_id`` values.

    Phase 1 fetched ``tickets.lead_id`` but never wrote it (the ``leads``
    table did not exist); this joins CRM tickets to the Phase 1 re-key map
    and stamps the column where the lead landed in sub.
    """
    step = "support_ticket_lead_ids"
    rows = _rows(
        crm,
        """
        SELECT id::text AS crm_ticket_id, lead_id::text AS lead_id
        FROM tickets
        WHERE lead_id IS NOT NULL
        ORDER BY id
        """,
    )
    ctx.stats.bump(step, "fetched", len(rows))
    leads_present = ctx.present_ids.get("leads", set())
    for row in rows:
        sub_ticket_id, dangling_ticket = rekey_ticket_id(
            row["crm_ticket_id"], ctx.ticket_map
        )
        if dangling_ticket or not sub_ticket_id:
            ctx.stats.bump(step, "skipped_unimported_ticket")
            continue
        lead_id = str(row["lead_id"]).lower()
        if lead_id not in leads_present:
            ctx.stats.bump(step, "dangling_lead")
            ctx.reports["dangling_lead_refs"].append(
                {
                    "table": "support_tickets",
                    "row_id": sub_ticket_id,
                    "crm_lead_id": lead_id,
                }
            )
            continue
        ctx.stats.bump(step, "stamped")
        if ctx.apply:
            sub.execute(
                text(
                    """
                    UPDATE support_tickets
                    SET lead_id = CAST(:lead_id AS uuid)
                    WHERE id = CAST(:id AS uuid)
                      AND lead_id IS DISTINCT FROM CAST(:lead_id AS uuid)
                    """
                ),
                {"id": sub_ticket_id, "lead_id": lead_id},
            )


def validate_lead_fk(sub: Connection, ctx: RunContext) -> None:
    """Deferred §3.5 step 3 / §1.10 promise: ``ALTER support_tickets ADD FK
    lead_id REFERENCES leads(id) NOT VALID`` then ``VALIDATE``. Idempotent."""
    step = "validate_lead_fk"
    constraint = _rows(
        sub,
        """
        SELECT convalidated
        FROM pg_constraint
        WHERE conrelid = 'support_tickets'::regclass AND conname = :name
        """,
        {"name": LEAD_FK_NAME},
    )
    if not constraint:
        ctx.stats.bump(step, "added_not_valid")
        if ctx.apply:
            sub.execute(
                text(
                    f"""
                    ALTER TABLE support_tickets
                    ADD CONSTRAINT {LEAD_FK_NAME}
                    FOREIGN KEY (lead_id) REFERENCES leads(id) NOT VALID
                    """
                )
            )
    elif constraint[0]["convalidated"]:
        ctx.stats.bump(step, "already_valid")
        return
    ctx.stats.bump(step, "validated")
    if ctx.apply:
        sub.execute(
            text(f"ALTER TABLE support_tickets VALIDATE CONSTRAINT {LEAD_FK_NAME}")
        )


def _import_quotes(sub: Connection, crm: Connection, ctx: RunContext) -> list[str]:
    step = "quotes"
    rows = _fetch(
        crm,
        """
        SELECT id::text, person_id::text, lead_id::text, owner_person_id::text,
               status::text, currency, subtotal, tax_rate, tax_total, total,
               expires_at, sent_at, notes, metadata::text, is_active,
               created_at, updated_at
        FROM crm_quotes
        """,
        since=ctx.since("crm_quotes"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_quotes", rows, "updated_at")
    existing = _load_existing_ids(sub, "quotes")
    _mark_present(ctx, "quotes", existing)
    leads_present = ctx.present_ids.get("leads", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        resolution = resolve_party_subscriber(
            row.get("person_id"),
            is_active=bool(row.get("is_active")),
            party_map=ctx.party_map,
        )
        if resolution.action == "block":
            ctx.block(
                step,
                {
                    "crm_id": row["id"],
                    "crm_person_id": row.get("person_id"),
                    "status": row.get("status"),
                    "reason": resolution.reason,
                },
            )
            continue
        if resolution.action == "skip":
            ctx.stats.bump(step, "skipped")
            ctx.reports["skipped_unresolved_inactive"].append(
                {
                    "table": "quotes",
                    "crm_id": row["id"],
                    "crm_person_id": row.get("person_id"),
                    "reason": resolution.reason,
                }
            )
            continue
        lead_id = _uuid_or_none(row.get("lead_id"))
        if lead_id and lead_id.lower() not in leads_present:
            ctx.stats.bump(step, "dangling_lead")
            ctx.reports["dangling_lead_refs"].append(
                {"table": "quotes", "row_id": row["id"], "crm_lead_id": lead_id}
            )
            lead_id = None
        ctx.note_staff("quotes", "owner_person_id", row.get("owner_person_id"))
        payloads.append(
            {
                "id": row["id"],
                "subscriber_id": resolution.subscriber_id,
                "lead_id": lead_id,
                "owner_person_id": row.get("owner_person_id"),
                "status": row.get("status") or "draft",
                "currency": row.get("currency") or "NGN",
                "subtotal": row.get("subtotal"),
                "tax_rate": row.get("tax_rate"),
                "tax_total": row.get("tax_total"),
                "total": row.get("total"),
                "expires_at": row.get("expires_at"),
                "sent_at": row.get("sent_at"),
                "notes": row.get("notes"),
                # §1.4: metadata verbatim (portal contract incl.
                # subscriber_external_id kept as provenance) + person link.
                "metadata": json.dumps(
                    provenance_metadata(
                        row.get("metadata"),
                        {"crm_person_id": row.get("person_id")},
                    )
                ),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS["quotes"], payloads, existing)
    return [str(row["id"]) for row in rows]


def _import_quote_line_items(
    sub: Connection, crm: Connection, ctx: RunContext, fetched_quote_ids: list[str]
) -> None:
    step = "quote_line_items"
    since = ctx.since("crm_quote_line_items")
    extra_where = None
    params: dict[str, Any] = {}
    if since is not None:
        # Incremental leg: lines of re-fetched quotes plus a created_at sweep
        # (CRM line rows have no updated_at of their own).
        extra_where = "(quote_id::text = ANY(:_quote_ids) OR created_at >= :_since)"
        params = {"_quote_ids": fetched_quote_ids, "_since": since}
    rows = _fetch(
        crm,
        """
        SELECT id::text, quote_id::text, inventory_item_id::text, description,
               quantity, unit_price, discount_percent, amount, metadata::text,
               created_at
        FROM crm_quote_line_items
        """,
        since=None,
        watermark_column="created_at",
        extra_where=extra_where,
        params=params,
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_quote_line_items", rows, "created_at")
    existing = _load_existing_ids(sub, "quote_line_items")
    quotes_present = ctx.present_ids.get("quotes", set())

    payloads: list[dict[str, Any]] = []
    kept_by_quote: dict[str, list[str]] = {}
    for row in rows:
        quote_id = str(row["quote_id"]).lower()
        if quote_id not in quotes_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        kept_by_quote.setdefault(quote_id, []).append(str(row["id"]))
        payloads.append(
            {
                "id": row["id"],
                "quote_id": row["quote_id"],
                "inventory_item_id": row.get("inventory_item_id"),
                "description": row.get("description") or "",
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
                "discount_percent": row.get("discount_percent"),
                "amount": row.get("amount"),
                "metadata": _verbatim_json(row.get("metadata"), None),
                "created_at": row["created_at"],
            }
        )
    # Hard-deleted CRM lines disappear from re-fetched quotes: prune sub rows
    # of those quotes that CRM no longer has.
    if ctx.apply and fetched_quote_ids:
        pruned = sub.execute(
            text(
                """
                DELETE FROM quote_line_items
                WHERE quote_id = ANY(CAST(:quote_ids AS uuid[]))
                  AND NOT (id = ANY(CAST(:keep_ids AS uuid[])))
                """
            ),
            {
                "quote_ids": fetched_quote_ids,
                "keep_ids": [
                    line_id for ids in kept_by_quote.values() for line_id in ids
                ],
            },
        )
        ctx.stats.bump(step, "pruned", int(pruned.rowcount or 0))
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["quote_line_items"], payloads, existing
    )


def _seed_sales_order_sequence(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    """§1.5/§3.5 step 5 head: continue the CRM ``SO-%06d`` sequence in sub's
    existing ``document_sequences`` table (key ``sales_order_number``)."""
    step = "sales_order_sequence"
    crm_rows = _rows(
        crm,
        "SELECT next_value FROM document_sequences WHERE key = :key",
        {"key": SALES_ORDER_SEQUENCE_KEY},
    )
    crm_next = int(crm_rows[0]["next_value"]) if crm_rows else None
    max_rows = _rows(crm, "SELECT order_number FROM sales_orders")
    max_number = None
    for row in max_rows:
        parsed = parse_order_number(row.get("order_number"))
        if parsed is not None and (max_number is None or parsed > max_number):
            max_number = parsed
    sub_rows = _rows(
        sub,
        "SELECT next_value FROM document_sequences WHERE key = :key",
        {"key": SALES_ORDER_SEQUENCE_KEY},
    )
    sub_next = int(sub_rows[0]["next_value"]) if sub_rows else None
    target = seed_sequence_next_value(
        sub_next_value=sub_next, crm_next_value=crm_next, max_order_number=max_number
    )
    ctx.stats.steps.setdefault(step, {})
    ctx.stats.steps[step]["crm_next_value"] = crm_next or 0
    ctx.stats.steps[step]["target_next_value"] = target
    if sub_next is None:
        ctx.stats.bump(step, "inserted")
        if ctx.apply:
            sub.execute(
                text(
                    """
                    INSERT INTO document_sequences (
                        id, key, next_value, created_at, updated_at
                    ) VALUES (CAST(:id AS uuid), :key, :next_value, now(), now())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "key": SALES_ORDER_SEQUENCE_KEY,
                    "next_value": target,
                },
            )
    elif target > sub_next:
        ctx.stats.bump(step, "advanced")
        if ctx.apply:
            sub.execute(
                text(
                    """
                    UPDATE document_sequences
                    SET next_value = :next_value, updated_at = now()
                    WHERE key = :key AND next_value < :next_value
                    """
                ),
                {"key": SALES_ORDER_SEQUENCE_KEY, "next_value": target},
            )
    else:
        ctx.stats.bump(step, "unchanged")


def _import_sales_orders(
    sub: Connection, crm: Connection, ctx: RunContext
) -> list[str]:
    step = "sales_orders"
    rows = _fetch(
        crm,
        """
        SELECT so.id::text, so.quote_id::text, so.person_id::text,
               so.owner_agent_id::text, so.source, so.order_number,
               so.status::text, so.payment_status::text, so.currency,
               so.subtotal, so.tax_total, so.total, so.amount_paid,
               so.balance_due, so.payment_due_date, so.paid_at,
               so.deposit_required, so.deposit_paid, so.contract_signed,
               so.signed_at, so.notes, so.metadata::text, so.is_active,
               so.created_at, so.updated_at,
               q.person_id::text AS quote_person_id
        FROM sales_orders so
        LEFT JOIN crm_quotes q ON q.id = so.quote_id
        """,
        since=ctx.since("crm_sales_orders"),
        watermark_column="so.updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_sales_orders", rows, "updated_at")
    existing = _load_existing_ids(sub, "sales_orders")
    _mark_present(ctx, "sales_orders", existing)
    quotes_present = ctx.present_ids.get("quotes", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        resolution, method = resolve_sales_order_subscriber(
            person_id=row.get("person_id"),
            quote_person_id=row.get("quote_person_id"),
            is_active=bool(row.get("is_active")),
            party_map=ctx.party_map,
        )
        if not resolution.subscriber_id:
            ctx.reports["unresolved_sales_orders"].append(
                {
                    "crm_id": row["id"],
                    "order_number": row.get("order_number"),
                    "crm_person_id": row.get("person_id"),
                    "crm_quote_person_id": row.get("quote_person_id"),
                    "status": row.get("status"),
                    "is_active": bool(row.get("is_active")),
                    "reason": resolution.reason,
                }
            )
            if resolution.action == "block":
                ctx.block(
                    step,
                    {
                        "crm_id": row["id"],
                        "order_number": row.get("order_number"),
                        "crm_person_id": row.get("person_id"),
                        "reason": resolution.reason,
                    },
                )
            else:
                ctx.stats.bump(step, "skipped")
            continue
        quote_id = _uuid_or_none(row.get("quote_id"))
        if quote_id and quote_id.lower() not in quotes_present:
            # Quote skipped (unresolved inactive) — keep the SO, drop the FK.
            ctx.stats.bump(step, "dangling_quote")
            quote_id = None
        payloads.append(
            {
                "id": row["id"],
                "quote_id": quote_id,
                "subscriber_id": resolution.subscriber_id,
                "owner_agent_id": row.get("owner_agent_id"),
                "source": row.get("source"),
                "order_number": row.get("order_number"),
                "status": row.get("status") or "draft",
                "payment_status": row.get("payment_status") or "pending",
                "currency": row.get("currency") or "NGN",
                "subtotal": row.get("subtotal"),
                "tax_total": row.get("tax_total"),
                "total": row.get("total"),
                "amount_paid": row.get("amount_paid"),
                "balance_due": row.get("balance_due"),
                "payment_due_date": row.get("payment_due_date"),
                "paid_at": row.get("paid_at"),
                "deposit_required": bool(row.get("deposit_required")),
                "deposit_paid": bool(row.get("deposit_paid")),
                "contract_signed": bool(row.get("contract_signed")),
                "signed_at": row.get("signed_at"),
                "notes": row.get("notes"),
                "metadata": json.dumps(
                    provenance_metadata(
                        row.get("metadata"),
                        {
                            "crm_person_id": row.get("person_id"),
                            "subscriber_resolution": method,
                        },
                    )
                ),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS["sales_orders"], payloads, existing)
    return [str(row["id"]) for row in rows]


def _import_sales_order_lines(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "sales_order_lines"
    rows = _fetch(
        crm,
        """
        SELECT id::text, sales_order_id::text, inventory_item_id::text,
               description, quantity, unit_price, amount, metadata::text,
               is_active, created_at, updated_at
        FROM sales_order_lines
        """,
        since=ctx.since("crm_sales_order_lines"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_sales_order_lines", rows, "updated_at")
    existing = _load_existing_ids(sub, "sales_order_lines")
    orders_present = ctx.present_ids.get("sales_orders", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["sales_order_id"]).lower() not in orders_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        payloads.append(
            {
                "id": row["id"],
                "sales_order_id": row["sales_order_id"],
                "inventory_item_id": row.get("inventory_item_id"),
                "description": row.get("description") or "",
                "quantity": row.get("quantity"),
                "unit_price": row.get("unit_price"),
                "amount": row.get("amount"),
                # §1.5: metadata verbatim — sub_offer_id and the
                # selfcare_subscription_* keys are local Fact post-import.
                "metadata": _verbatim_json(row.get("metadata"), None),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["sales_order_lines"], payloads, existing
    )


def _backfill_subscriber_sales_order_ids(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    """§3.5 step 6: ``subscribers.sales_order_id`` via link key 2.

    Only stamps NULLs; a differing existing value is reported, never
    overwritten (house rule from backfill_crm_subscriber_links.py).
    """
    step = "subscriber_sales_order_ids"
    rows = _rows(
        crm,
        """
        SELECT id::text AS crm_subscriber_id, sales_order_id::text
        FROM subscribers
        WHERE sales_order_id IS NOT NULL
        ORDER BY id
        """,
    )
    ctx.stats.bump(step, "fetched", len(rows))
    orders_present = ctx.present_ids.get("sales_orders", set())
    sub_values = {
        str(row["id"]).lower(): (
            str(row["sales_order_id"]).lower() if row.get("sales_order_id") else None
        )
        for row in _rows(
            sub,
            """
            SELECT id::text AS id, sales_order_id::text AS sales_order_id
            FROM subscribers
            WHERE crm_subscriber_id IS NOT NULL OR sales_order_id IS NOT NULL
            """,
        )
    }
    for row in rows:
        subscriber_id = ctx.subscriber_map.get(str(row["crm_subscriber_id"]))
        if not subscriber_id:
            ctx.stats.bump(step, "unmapped_subscriber")
            continue
        sales_order_id = str(row["sales_order_id"]).lower()
        if sales_order_id not in orders_present:
            ctx.stats.bump(step, "dangling_sales_order")
            continue
        current = sub_values.get(subscriber_id.lower())
        if current == sales_order_id:
            ctx.stats.bump(step, "unchanged")
            continue
        if current:
            ctx.stats.bump(step, "mismatch")
            ctx.reports["subscriber_sales_order_mismatch"].append(
                {
                    "subscriber_id": subscriber_id,
                    "existing_sales_order_id": current,
                    "crm_sales_order_id": sales_order_id,
                }
            )
            continue
        ctx.stats.bump(step, "stamped")
        if ctx.apply:
            sub.execute(
                text(
                    """
                    UPDATE subscribers
                    SET sales_order_id = CAST(:sales_order_id AS uuid)
                    WHERE id = CAST(:id AS uuid) AND sales_order_id IS NULL
                    """
                ),
                {"id": subscriber_id, "sales_order_id": sales_order_id},
            )


def _import_project_templates(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    step = "project_templates"
    rows = _fetch(
        crm,
        """
        SELECT id::text, name, project_type::text, description, is_active,
               created_at, updated_at
        FROM project_templates
        """,
        since=ctx.since("crm_project_templates"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_project_templates", rows, "updated_at")
    existing = _load_existing_ids(sub, "project_templates")
    _mark_present(ctx, "project_templates", existing)
    payloads = [
        {
            "id": row["id"],
            "name": row["name"],
            "project_type": row.get("project_type"),
            "description": row.get("description"),
            "is_active": bool(row.get("is_active")),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["project_templates"], payloads, existing
    )


def _import_project_template_tasks(
    sub: Connection, crm: Connection, ctx: RunContext
) -> list[str]:
    step = "project_template_tasks"
    rows = _fetch(
        crm,
        """
        SELECT id::text, template_id::text, title, description, status::text,
               priority::text, sort_order, effort_hours, is_active,
               created_at, updated_at
        FROM project_template_tasks
        """,
        since=ctx.since("crm_project_template_tasks"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_project_template_tasks", rows, "updated_at")
    existing = _load_existing_ids(sub, "project_template_tasks")
    _mark_present(ctx, "project_template_tasks", existing)
    templates_present = ctx.present_ids.get("project_templates", set())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["template_id"]).lower() not in templates_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        payloads.append(
            {
                "id": row["id"],
                "template_id": row["template_id"],
                "title": row["title"],
                "description": row.get("description"),
                "status": row.get("status"),
                "priority": row.get("priority"),
                "sort_order": row.get("sort_order"),
                "effort_hours": row.get("effort_hours"),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["project_template_tasks"], payloads, existing
    )
    return [str(row["id"]) for row in rows]


def _import_project_template_task_dependency(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    # No timestamps on the CRM table — always a full fetch (small table).
    step = "project_template_task_dependency"
    rows = _rows(
        crm,
        """
        SELECT id::text, template_task_id::text,
               depends_on_template_task_id::text, dependency_type::text,
               lag_days
        FROM project_template_task_dependency
        ORDER BY id
        """,
    )
    ctx.stats.bump(step, "fetched", len(rows))
    existing = _load_existing_ids(sub, "project_template_task_dependency")
    tasks_present = ctx.present_ids.get("project_template_tasks", set())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if (
            str(row["template_task_id"]).lower() not in tasks_present
            or str(row["depends_on_template_task_id"]).lower() not in tasks_present
        ):
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        payloads.append(
            {
                "id": row["id"],
                "template_task_id": row["template_task_id"],
                "depends_on_template_task_id": row["depends_on_template_task_id"],
                "dependency_type": row.get("dependency_type") or "finish_to_start",
                "lag_days": row.get("lag_days"),
            }
        )
    _execute_upserts(
        sub,
        ctx,
        step,
        TABLE_SPECS["project_template_task_dependency"],
        payloads,
        existing,
    )


def _import_projects(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "projects"
    if ctx.apply:
        # FK target refresh (Phase 1 table, same idempotent upsert).
        ctx.stats.bump(step, "service_teams_upserted", _upsert_service_teams(sub, crm))
    rows = _fetch(
        crm,
        """
        SELECT id::text, name, code, number, erpnext_id, description,
               customer_address, project_type::text, project_template_id::text,
               status::text, priority::text, subscriber_id::text, lead_id::text,
               created_by_person_id::text, owner_person_id::text,
               manager_person_id::text, project_manager_person_id::text,
               assistant_manager_person_id::text, service_team_id::text,
               start_at, due_at, completed_at, region, tags::text,
               metadata::text, is_active, created_at, updated_at
        FROM projects
        """,
        since=ctx.since("crm_projects"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_projects", rows, "updated_at")
    existing = _load_existing_ids(sub, "projects")
    _mark_present(ctx, "projects", existing)
    leads_present = ctx.present_ids.get("leads", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        # Nullable in both trees: unresolved subscribers import unlinked and
        # surface in the drift checker's unresolved_subscribers class.
        subscriber_id = None
        crm_subscriber_id = _uuid_or_none(row.get("subscriber_id"))
        if crm_subscriber_id:
            subscriber_id = ctx.subscriber_map.get(crm_subscriber_id)
            if not subscriber_id:
                ctx.stats.bump(step, "unresolved_subscriber")
        lead_id = _uuid_or_none(row.get("lead_id"))
        if lead_id and lead_id.lower() not in leads_present:
            ctx.stats.bump(step, "dangling_lead")
            ctx.reports["dangling_lead_refs"].append(
                {"table": "projects", "row_id": row["id"], "crm_lead_id": lead_id}
            )
            lead_id = None
        for column in (
            "created_by_person_id",
            "owner_person_id",
            "manager_person_id",
            "project_manager_person_id",
            "assistant_manager_person_id",
        ):
            ctx.note_staff("projects", column, row.get(column))
        payloads.append(
            {
                "id": row["id"],
                "name": row["name"],
                "code": row.get("code"),
                "number": row.get("number"),
                "erpnext_id": row.get("erpnext_id"),
                "description": row.get("description"),
                "customer_address": row.get("customer_address"),
                "project_type": row.get("project_type"),
                "project_template_id": row.get("project_template_id"),
                "status": row.get("status") or "open",
                "priority": row.get("priority") or "normal",
                "subscriber_id": subscriber_id,
                "lead_id": lead_id,
                "created_by_person_id": row.get("created_by_person_id"),
                "owner_person_id": row.get("owner_person_id"),
                "manager_person_id": row.get("manager_person_id"),
                "project_manager_person_id": row.get("project_manager_person_id"),
                "assistant_manager_person_id": row.get("assistant_manager_person_id"),
                "service_team_id": row.get("service_team_id"),
                "start_at": row.get("start_at"),
                "due_at": row.get("due_at"),
                "completed_at": row.get("completed_at"),
                "region": row.get("region"),
                "tags": _verbatim_json(row.get("tags"), None),
                "metadata": _verbatim_json(row.get("metadata"), None),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS["projects"], payloads, existing)


def _import_project_tasks(
    sub: Connection, crm: Connection, ctx: RunContext
) -> list[str]:
    """Two-phase task apply: upsert rows without ``parent_task_id`` first,
    then re-link parents once every row exists (self-FK ordering, house
    pattern from backfill_crm_subscriber_links.py)."""
    step = "project_tasks"
    rows = _fetch(
        crm,
        """
        SELECT id::text, project_id::text, parent_task_id::text, title, number,
               erpnext_id, description, template_task_id::text, status::text,
               priority::text, assigned_to_person_id::text,
               created_by_person_id::text, ticket_id::text, work_order_id::text,
               start_at, due_at, completed_at, effort_hours, tags::text,
               metadata::text, is_active, created_at, updated_at
        FROM project_tasks
        """,
        since=ctx.since("crm_project_tasks"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_project_tasks", rows, "updated_at")
    existing = _load_existing_ids(sub, "project_tasks")
    _mark_present(ctx, "project_tasks", existing)
    projects_present = ctx.present_ids.get("projects", set())
    template_tasks_present = ctx.present_ids.get("project_template_tasks", set())

    payloads: list[dict[str, Any]] = []
    parent_links: list[dict[str, str]] = []
    for row in rows:
        if str(row["project_id"]).lower() not in projects_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        ticket_id, dangling = rekey_ticket_id(row.get("ticket_id"), ctx.ticket_map)
        if dangling:
            ctx.stats.bump(step, "dangling_ticket")
            ctx.reports["dangling_ticket_refs"].append(
                {
                    "table": "project_tasks",
                    "row_id": row["id"],
                    "crm_ticket_id": row.get("ticket_id"),
                }
            )
        template_task_id = _uuid_or_none(row.get("template_task_id"))
        if template_task_id and template_task_id.lower() not in template_tasks_present:
            ctx.stats.bump(step, "dangling_template_task")
            template_task_id = None
        for column in ("assigned_to_person_id", "created_by_person_id"):
            ctx.note_staff("project_tasks", column, row.get(column))
        payloads.append(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "title": row["title"],
                "number": row.get("number"),
                "erpnext_id": row.get("erpnext_id"),
                "description": row.get("description"),
                "template_task_id": template_task_id,
                "status": row.get("status") or "todo",
                "priority": row.get("priority") or "normal",
                "assigned_to_person_id": row.get("assigned_to_person_id"),
                "created_by_person_id": row.get("created_by_person_id"),
                "ticket_id": ticket_id,
                # Plain UUID until the Phase 2 flip adds the FK (§1.2).
                "work_order_id": row.get("work_order_id"),
                "start_at": row.get("start_at"),
                "due_at": row.get("due_at"),
                "completed_at": row.get("completed_at"),
                "effort_hours": row.get("effort_hours"),
                "tags": _verbatim_json(row.get("tags"), None),
                # fiber_stage_key/fiber_sla_* keys preserved verbatim (§1.2).
                "metadata": _verbatim_json(row.get("metadata"), None),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        if row.get("parent_task_id"):
            parent_links.append(
                {"id": str(row["id"]), "parent_task_id": str(row["parent_task_id"])}
            )

    _execute_upserts(sub, ctx, step, TABLE_SPECS["project_tasks"], payloads, existing)

    tasks_present = ctx.present_ids.get("project_tasks", set())
    for link in parent_links:
        if link["parent_task_id"].lower() not in tasks_present:
            ctx.stats.bump(step, "dangling_parent_task")
            continue
        ctx.stats.bump(step, "parent_linked")
        if ctx.apply:
            sub.execute(
                text(
                    """
                    UPDATE project_tasks
                    SET parent_task_id = CAST(:parent_task_id AS uuid)
                    WHERE id = CAST(:id AS uuid)
                      AND parent_task_id IS DISTINCT FROM
                          CAST(:parent_task_id AS uuid)
                    """
                ),
                link,
            )
    return [str(payload["id"]) for payload in payloads]


def _import_project_task_assignees(
    sub: Connection, crm: Connection, ctx: RunContext, fetched_task_ids: list[str]
) -> None:
    """Assignment facts (composite PK, hard-deleted in CRM): delete + insert
    per fetched task, phase1 ``_sync_ticket_assignees`` pattern."""
    step = "project_task_assignees"
    if not fetched_task_ids:
        return
    rows = _rows(
        crm,
        """
        SELECT task_id::text, person_id::text, created_at
        FROM project_task_assignees
        WHERE task_id::text = ANY(:task_ids)
        ORDER BY created_at, task_id, person_id
        """,
        {"task_ids": fetched_task_ids},
    )
    ctx.stats.bump(step, "fetched", len(rows))
    if ctx.apply:
        sub.execute(
            text(
                """
                DELETE FROM project_task_assignees
                WHERE task_id = ANY(CAST(:task_ids AS uuid[]))
                """
            ),
            {"task_ids": fetched_task_ids},
        )
    tasks_present = ctx.present_ids.get("project_tasks", set())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row["task_id"]).lower() not in tasks_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        ctx.note_staff("project_task_assignees", "person_id", row.get("person_id"))
        payloads.append(
            {
                "task_id": row["task_id"],
                "person_id": row["person_id"],
                "created_at": row["created_at"],
            }
        )
    _execute_upserts(
        sub,
        ctx,
        step,
        TABLE_SPECS["project_task_assignees"],
        payloads,
        set(),
        id_column="task_id",
    )


def _import_project_task_dependencies(
    sub: Connection, crm: Connection, ctx: RunContext
) -> None:
    # No timestamps on the CRM table — always a full fetch.
    step = "project_task_dependencies"
    rows = _rows(
        crm,
        """
        SELECT id::text, task_id::text, depends_on_task_id::text,
               dependency_type::text, lag_days
        FROM project_task_dependencies
        ORDER BY id
        """,
    )
    ctx.stats.bump(step, "fetched", len(rows))
    existing = _load_existing_ids(sub, "project_task_dependencies")
    tasks_present = ctx.present_ids.get("project_tasks", set())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if (
            str(row["task_id"]).lower() not in tasks_present
            or str(row["depends_on_task_id"]).lower() not in tasks_present
        ):
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        payloads.append(
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "depends_on_task_id": row["depends_on_task_id"],
                "dependency_type": row.get("dependency_type") or "finish_to_start",
                "lag_days": row.get("lag_days"),
            }
        )
    _execute_upserts(
        sub, ctx, step, TABLE_SPECS["project_task_dependencies"], payloads, existing
    )


def _import_comments(
    sub: Connection,
    crm: Connection,
    ctx: RunContext,
    *,
    step: str,
    crm_table: str,
    parent_column: str,
    parent_table: str,
    fetched_parent_ids: list[str],
) -> None:
    """Shared path for project_task_comments / project_comments: insert-only
    (``DO NOTHING``) with provenance metadata added per §1.2, fetched by
    parent or created_at sweep."""
    since = ctx.since(f"crm_{crm_table}")
    extra_where = None
    params: dict[str, Any] = {}
    if since is not None:
        extra_where = (
            f"({parent_column}::text = ANY(:_parent_ids) OR created_at >= :_since)"
        )
        params = {"_parent_ids": fetched_parent_ids, "_since": since}
    rows = _fetch(
        crm,
        f"""
        SELECT id::text, {parent_column}::text, author_person_id::text, body,
               attachments::text, created_at
        FROM {crm_table}
        """,  # noqa: S608
        since=None,
        watermark_column="created_at",
        extra_where=extra_where,
        params=params,
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark(f"crm_{crm_table}", rows, "created_at")
    existing = _load_existing_ids(sub, crm_table)
    parents_present = ctx.present_ids.get(parent_table, set())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if str(row[parent_column]).lower() not in parents_present:
            ctx.stats.bump(step, "skipped_unimported_parent")
            continue
        ctx.note_staff(crm_table, "author_person_id", row.get("author_person_id"))
        payloads.append(
            {
                "id": row["id"],
                parent_column: row[parent_column],
                "author_person_id": row.get("author_person_id"),
                "body": row.get("body") or "",
                "attachments": _verbatim_json(row.get("attachments"), None),
                "metadata": json.dumps(
                    provenance_metadata(
                        None, {"crm_author_person_id": row.get("author_person_id")}
                    )
                ),
                "created_at": row["created_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS[crm_table], payloads, existing)


def _import_referral_codes(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "referral_codes"
    rows = _fetch(
        crm,
        """
        SELECT id::text, person_id::text, code, is_active, created_at
        FROM referral_codes
        """,
        since=ctx.since("crm_referral_codes"),
        watermark_column="created_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_referral_codes", rows, "created_at")
    existing = _load_existing_ids(sub, "referral_codes")
    _mark_present(ctx, "referral_codes", existing)
    payloads: list[dict[str, Any]] = []
    for row in rows:
        resolution = resolve_party_subscriber(
            row.get("person_id"),
            is_active=bool(row.get("is_active")),
            party_map=ctx.party_map,
        )
        if resolution.action == "block":
            ctx.block(
                step,
                {
                    "crm_id": row["id"],
                    "crm_person_id": row.get("person_id"),
                    "code": row.get("code"),
                    "reason": resolution.reason,
                },
            )
            continue
        if resolution.action == "skip":
            ctx.stats.bump(step, "skipped")
            ctx.reports["skipped_unresolved_inactive"].append(
                {
                    "table": "referral_codes",
                    "crm_id": row["id"],
                    "crm_person_id": row.get("person_id"),
                    "reason": resolution.reason,
                }
            )
            continue
        payloads.append(
            {
                "id": row["id"],
                "subscriber_id": resolution.subscriber_id,
                "code": row["code"],
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS["referral_codes"], payloads, existing)


def _import_referrals(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "referrals"
    rows = _fetch(
        crm,
        """
        SELECT id::text, referrer_person_id::text, referral_code_id::text,
               referred_person_id::text, referred_lead_id::text,
               referred_subscriber_id::text, status::text, reward_amount,
               reward_currency, reward_status::text, reward_issued_at,
               qualified_at, source, notes, metadata::text, is_active,
               created_at, updated_at
        FROM referrals
        """,
        since=ctx.since("crm_referrals"),
        watermark_column="updated_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_referrals", rows, "updated_at")
    existing = _load_existing_ids(sub, "referrals")
    _mark_present(ctx, "referrals", existing)
    codes_present = ctx.present_ids.get("referral_codes", set())
    leads_present = ctx.present_ids.get("leads", set())

    payloads: list[dict[str, Any]] = []
    for row in rows:
        referrer = resolve_party_subscriber(
            row.get("referrer_person_id"),
            is_active=bool(row.get("is_active")),
            party_map=ctx.party_map,
        )
        if referrer.action == "block":
            ctx.block(
                step,
                {
                    "crm_id": row["id"],
                    "crm_person_id": row.get("referrer_person_id"),
                    "status": row.get("status"),
                    "reason": referrer.reason,
                },
            )
            continue
        if referrer.action == "skip":
            ctx.stats.bump(step, "skipped")
            ctx.reports["skipped_unresolved_inactive"].append(
                {
                    "table": "referrals",
                    "crm_id": row["id"],
                    "crm_person_id": row.get("referrer_person_id"),
                    "reason": referrer.reason,
                }
            )
            continue
        referred_subscriber_id, disagreement = resolve_referred_subscriber(
            referred_person_id=row.get("referred_person_id"),
            crm_referred_subscriber_id=row.get("referred_subscriber_id"),
            party_map=ctx.party_map,
            subscriber_map=ctx.subscriber_map,
        )
        if disagreement:
            ctx.stats.bump(step, "referred_disagreement")
            ctx.reports["referred_link_disagreements"].append(
                {"crm_id": row["id"], **disagreement}
            )
        referral_code_id = _uuid_or_none(row.get("referral_code_id"))
        if referral_code_id and referral_code_id.lower() not in codes_present:
            ctx.stats.bump(step, "dangling_referral_code")
            referral_code_id = None
        referred_lead_id = _uuid_or_none(row.get("referred_lead_id"))
        if referred_lead_id and referred_lead_id.lower() not in leads_present:
            ctx.stats.bump(step, "dangling_lead")
            ctx.reports["dangling_lead_refs"].append(
                {
                    "table": "referrals",
                    "row_id": row["id"],
                    "crm_lead_id": referred_lead_id,
                }
            )
            referred_lead_id = None
        payloads.append(
            {
                "id": row["id"],
                "referrer_subscriber_id": referrer.subscriber_id,
                "referral_code_id": referral_code_id,
                "referred_subscriber_id": referred_subscriber_id,
                "referred_lead_id": referred_lead_id,
                "status": row.get("status") or "pending",
                "reward_amount": row.get("reward_amount"),
                "reward_currency": row.get("reward_currency") or "NGN",
                "reward_status": row.get("reward_status") or "none",
                "reward_issued_at": row.get("reward_issued_at"),
                "qualified_at": row.get("qualified_at"),
                "source": row.get("source"),
                "notes": row.get("notes"),
                "metadata": json.dumps(
                    provenance_metadata(
                        row.get("metadata"),
                        {
                            "crm_referrer_person_id": row.get("referrer_person_id"),
                            "crm_referred_person_id": row.get("referred_person_id"),
                            "crm_referred_subscriber_id": row.get(
                                "referred_subscriber_id"
                            ),
                        },
                    )
                ),
                "is_active": bool(row.get("is_active")),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    conflicts = plan_referral_referred_unique_conflicts(payloads)
    for conflict in conflicts:
        ctx.reports["referral_referred_unique_conflicts"].append(conflict)
        ctx.block(step, {"reason": "referral_referred_unique_conflict", **conflict})
    if conflicts:
        return
    _execute_upserts(sub, ctx, step, TABLE_SPECS["referrals"], payloads, existing)


def _import_work_links(sub: Connection, crm: Connection, ctx: RunContext) -> None:
    step = "work_links"
    rows = _fetch(
        crm,
        """
        SELECT id::text, source_type::text, source_id::text, target_type::text,
               target_id::text, link_type::text, contract_name,
               created_by_person_id::text, metadata::text, created_at
        FROM work_links
        """,
        since=ctx.since("crm_work_links"),
        watermark_column="created_at",
    )
    ctx.stats.bump(step, "fetched", len(rows))
    ctx.note_watermark("crm_work_links", rows, "created_at")
    existing = _load_existing_ids(sub, "work_links")
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if not work_link_is_phase3(row.get("source_type"), row.get("target_type")):
            ctx.stats.bump(step, "deferred")
            ctx.reports["deferred_work_links"].append(
                {
                    "crm_id": row["id"],
                    "source_type": row.get("source_type"),
                    "target_type": row.get("target_type"),
                    "link_type": row.get("link_type"),
                }
            )
            continue
        ctx.note_staff(
            "work_links", "created_by_person_id", row.get("created_by_person_id")
        )
        payloads.append(
            {
                "id": row["id"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "link_type": row["link_type"],
                "contract_name": row.get("contract_name"),
                "created_by_person_id": row.get("created_by_person_id"),
                "metadata": _verbatim_json(row.get("metadata"), None),
                "created_at": row["created_at"],
            }
        )
    _execute_upserts(sub, ctx, step, TABLE_SPECS["work_links"], payloads, existing)


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


def run_import(
    *,
    sub: Connection,
    crm: Connection,
    ctx: RunContext,
    validate_lead_fk_flag: bool,
) -> ImportStats:
    _import_pipelines(sub, crm, ctx)
    _import_pipeline_stages(sub, crm, ctx)
    _import_leads(sub, crm, ctx)
    _backfill_support_ticket_lead_ids(sub, crm, ctx)
    fetched_quote_ids = _import_quotes(sub, crm, ctx)
    _import_quote_line_items(sub, crm, ctx, fetched_quote_ids)
    _seed_sales_order_sequence(sub, crm, ctx)
    _import_sales_orders(sub, crm, ctx)
    _import_sales_order_lines(sub, crm, ctx)
    _backfill_subscriber_sales_order_ids(sub, crm, ctx)
    _import_project_templates(sub, crm, ctx)
    _import_project_template_tasks(sub, crm, ctx)
    _import_project_template_task_dependency(sub, crm, ctx)
    _import_projects(sub, crm, ctx)
    fetched_task_ids = _import_project_tasks(sub, crm, ctx)
    _import_project_task_assignees(sub, crm, ctx, fetched_task_ids)
    _import_project_task_dependencies(sub, crm, ctx)
    _import_comments(
        sub,
        crm,
        ctx,
        step="project_task_comments",
        crm_table="project_task_comments",
        parent_column="task_id",
        parent_table="project_tasks",
        fetched_parent_ids=fetched_task_ids,
    )
    _import_comments(
        sub,
        crm,
        ctx,
        step="project_comments",
        crm_table="project_comments",
        parent_column="project_id",
        parent_table="projects",
        fetched_parent_ids=[],
    )
    _import_referral_codes(sub, crm, ctx)
    _import_referrals(sub, crm, ctx)
    _import_work_links(sub, crm, ctx)
    if validate_lead_fk_flag and not ctx.stats.blockers:
        validate_lead_fk(sub, ctx)
    _report_unmapped_staff(ctx)
    return ctx.stats


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--party-map",
        help=(
            "person_subscriber_map.csv from backfill_party_status.py "
            "(crm_person_id -> subscriber_id); merged over the links already "
            "stamped in sub metadata, CSV rows win."
        ),
    )
    parser.add_argument(
        "--staff-map",
        help=(
            "staff_map.csv from build_crm_staff_map.py; staff UUIDs carry "
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
        "--validate-lead-fk",
        action="store_true",
        help=(
            "After the lead_id backfill, ADD the support_tickets.lead_id FK "
            "NOT VALID and VALIDATE it (deferred §3.5 step 3; run once the "
            "leads import is green)."
        ),
    )
    parser.add_argument(
        "--out",
        default="phase3-import-report",
        help="Directory for the summary JSON and per-action CSVs.",
    )
    args = parser.parse_args()

    out = Path(args.out)
    party_map_csv = _load_party_map_csv(args.party_map)
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
                party_map=merge_party_maps(
                    party_map_csv, _load_party_map_from_sub(sub)
                ),
                subscriber_map=_load_subscriber_map(sub),
                staff_map=staff_map,
                ticket_map=_load_ticket_rekey_map(sub),
            )
            stats = run_import(
                sub=sub,
                crm=crm,
                ctx=ctx,
                validate_lead_fk_flag=args.validate_lead_fk,
            )
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
        "party_map": args.party_map,
        "party_map_entries": len(ctx.party_map),
        "staff_map": args.staff_map,
        "staff_map_entries": len(staff_map),
        "state_file": args.state_file,
        "state_overlap_seconds": args.state_overlap_seconds,
        "validate_lead_fk": args.validate_lead_fk,
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
