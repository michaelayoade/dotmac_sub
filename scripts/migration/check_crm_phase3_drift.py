#!/usr/bin/env python3
"""Phase 3 drift checker — gate to the flip (20-phase3-projects-sales.md §3.6).

Read-only comparison of the four CRM verticals (projects family, leads/
pipeline, quotes, sales orders, referrals + work links) against the native
sub tables written by ``import_crm_phase3.py``. Rows join on the shared CRM
UUID (§3.4 — CRM ids are sub PKs), so there is no marker metadata to trust:
the PK itself is the join.

Finding classes:
  * ``crm_missing_in_sub`` — CRM rows with no sub row of the same id; rows
    the importer's own policy skips (inactive party rows whose person the
    party map cannot resolve) classify as ``skipped_unresolved_inactive``
    instead and do not gate;
  * ``sub_orphans`` — sub rows whose id no longer exists in CRM;
  * ``field_drift`` — the §3.6 field lists: projects (name, status, type,
    subscriber link, five role UUIDs, dates, region), leads (subscriber
    link, status, stage, value, source), quotes (status, totals, deposit
    metadata), sales orders (order_number, totals, payment fields),
    referrals (status + reward fields, collapsed referred link),
    referral_codes (code, subscriber link);
  * ``children_count_mismatch`` — quote/SO line count + amount sum; per-
    project task/comment/assignee/dependency/task-comment counts;
  * ``so_sequence`` — §3.6/§1.5 numbering: the ``SO-%06d`` number sets must
    match on both sides and sub's ``document_sequences`` row must clear both
    CRM's ``next_value`` and the highest imported number (risk #10);
  * ``sub_enrichment`` — informational, non-gating: sub values deliberately
    richer than CRM's (the importer's provenance metadata keys; a resolved
    referred-subscriber where CRM held only the person link);
  * ``referred_link_disagreements`` — §3.6 cross-check of
    ``referred_person_id``-resolved vs ``referred_subscriber_id``-resolved
    subscribers (informational; the CSV is the review artifact);
  * ``idempotency_triangle`` — quote -> SO -> project consistency both
    directions via ``projects.metadata`` ``quote_id``/``sales_order_id`` and
    ``sales_orders.quote_id`` (informational: the importer copies CRM
    metadata verbatim, so a broken triangle is a CRM data fact, not import
    drift);
  * ``subscriber_sales_order_asymmetry`` — ``subscribers.sales_order_id``
    rows whose sales order links a different subscriber (informational);
  * ``unresolved_subscribers`` / ``unmapped_staff`` — informational orphan
    CSVs (§3.6 tail).

Rows whose CRM ``updated_at`` (``created_at`` for insert-only tables) falls
within ``--updated-within-minutes`` (default 30) count as
``expected_in_flight`` — webhooks + reconcile keep the window live pre-flip
(§3.5 tail) — and do not gate.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Both sessions are forced into READ ONLY transactions and rolled back; the
checker never writes. Output: summary JSON on stdout plus one CSV per class
in ``--out``. Exit code 0 when there is zero gating drift outside the live
window, 1 otherwise — cron/CI gates the flip on two consecutive clean runs.
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
from scripts.migration.import_crm_phase3 import (  # noqa: E402
    SALES_ORDER_SEQUENCE_KEY,
    STAFF_UUID_COLUMNS,
    _load_party_map_csv,
    _load_party_map_from_sub,
    merge_party_maps,
    parse_order_number,
    resolve_referred_subscriber,
    work_link_is_phase3,
)
from scripts.migration.import_crm_tickets_phase1 import (  # noqa: E402
    _engine_from_env,
    _format_datetime,
    _load_staff_map,
    _load_subscriber_map,
    _parse_datetime,
    _rows,
    _uuid_or_none,
)

DEFAULT_UPDATED_WITHIN_MINUTES = 30

GATING_CLASSES = (
    "crm_missing_in_sub",
    "sub_orphans",
    "field_drift",
    "children_count_mismatch",
    "so_sequence",
)

INFO_CLASSES = (
    "sub_enrichment",
    "expected_in_flight",
    "skipped_unresolved_inactive",
    "unresolved_subscribers",
    "unmapped_staff",
    "referred_link_disagreements",
    "idempotency_triangle",
    "subscriber_sales_order_asymmetry",
)

ALL_CLASSES = GATING_CLASSES + INFO_CLASSES

# (table, live-window timestamp column) for the generic id-set pass. Tables
# with a NOT NULL subscriber collapse (see _PARTY_PERSON_COLUMN) additionally
# get importer-policy classification for missing rows.
ID_SET_TABLES: tuple[tuple[str, str | None], ...] = (
    ("pipelines", "updated_at"),
    ("pipeline_stages", "updated_at"),
    ("leads", "updated_at"),
    ("quotes", "updated_at"),
    ("quote_line_items", "created_at"),
    ("sales_orders", "updated_at"),
    ("sales_order_lines", "updated_at"),
    ("project_templates", "updated_at"),
    ("project_template_tasks", "updated_at"),
    ("project_template_task_dependency", None),
    ("projects", "updated_at"),
    ("project_tasks", "updated_at"),
    ("project_task_dependencies", None),
    ("project_task_comments", "created_at"),
    ("project_comments", "created_at"),
    ("referral_codes", "created_at"),
    ("referrals", "updated_at"),
    ("work_links", "created_at"),
)

PROJECT_ROLE_COLUMNS = (
    "created_by_person_id",
    "owner_person_id",
    "manager_person_id",
    "project_manager_person_id",
    "assistant_manager_person_id",
)

PROJECT_DATE_COLUMNS = ("start_at", "due_at", "completed_at")

SO_PAYMENT_COLUMNS = (
    "payment_status",
    "deposit_required",
    "deposit_paid",
)

REFERRAL_REWARD_COLUMNS = ("reward_status", "reward_currency")


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


def _norm_ts(value: Any) -> str | None:
    return _format_datetime(_parse_datetime(value))


def _metadata_dict(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _diff(diffs: list[FieldDiff], field: str, crm_value: Any, sub_value: Any) -> None:
    if crm_value != sub_value:
        diffs.append(
            FieldDiff(
                field,
                None if crm_value is None else str(crm_value),
                None if sub_value is None else str(sub_value),
            )
        )


# ---------------------------------------------------------------------------
# Pure per-vertical comparisons (§3.6 field lists)
# ---------------------------------------------------------------------------


def resolve_party_expectation(
    person_id: str | None, party_map: dict[str, str]
) -> str | None:
    key = (person_id or "").strip().lower()
    return party_map.get(key) if key else None


def compare_lead_fields(
    crm_row: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    party_map: dict[str, str],
) -> RowComparison:
    """Leads: subscriber link, status, stage, value, source (§3.6)."""
    diffs: list[FieldDiff] = []
    _diff(
        diffs,
        "status",
        _norm_text(crm_row.get("status")),
        _norm_text(sub_row.get("status")),
    )
    _diff(
        diffs,
        "stage_id",
        _norm_uuid(crm_row.get("stage_id")),
        _norm_uuid(sub_row.get("stage_id")),
    )
    _diff(
        diffs,
        "estimated_value",
        _norm_decimal(crm_row.get("estimated_value")),
        _norm_decimal(sub_row.get("estimated_value")),
    )
    _diff(
        diffs,
        "lead_source",
        _norm_text(crm_row.get("lead_source")),
        _norm_text(sub_row.get("lead_source")),
    )

    unresolved = None
    expected = resolve_party_expectation(crm_row.get("person_id"), party_map)
    if expected:
        _diff(
            diffs,
            "subscriber_id",
            expected.lower(),
            _norm_uuid(sub_row.get("subscriber_id")),
        )
    else:
        unresolved = "unresolved_person"
    return RowComparison(tuple(diffs), (), unresolved)


def compare_quote_fields(
    crm_row: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    party_map: dict[str, str],
) -> RowComparison:
    """Quotes: status, totals, deposit metadata (§3.6)."""
    diffs: list[FieldDiff] = []
    enrichments: list[FieldDiff] = []
    _diff(
        diffs,
        "status",
        _norm_text(crm_row.get("status")),
        _norm_text(sub_row.get("status")),
    )
    for column in ("subtotal", "tax_total", "total"):
        _diff(
            diffs,
            column,
            _norm_decimal(crm_row.get(column)),
            _norm_decimal(sub_row.get(column)),
        )

    crm_deposit = _metadata_dict(crm_row.get("metadata")).get("deposit")
    sub_deposit = _metadata_dict(sub_row.get("metadata")).get("deposit")
    if crm_deposit != sub_deposit:
        if crm_deposit is None and sub_deposit is not None:
            enrichments.append(
                FieldDiff("metadata.deposit", None, json.dumps(sub_deposit))
            )
        else:
            diffs.append(
                FieldDiff(
                    "metadata.deposit",
                    json.dumps(crm_deposit, sort_keys=True, default=str),
                    json.dumps(sub_deposit, sort_keys=True, default=str),
                )
            )

    unresolved = None
    expected = resolve_party_expectation(crm_row.get("person_id"), party_map)
    if expected:
        _diff(
            diffs,
            "subscriber_id",
            expected.lower(),
            _norm_uuid(sub_row.get("subscriber_id")),
        )
    else:
        unresolved = "unresolved_person"
    return RowComparison(tuple(diffs), tuple(enrichments), unresolved)


def compare_sales_order_fields(
    crm_row: dict[str, Any], sub_row: dict[str, Any]
) -> RowComparison:
    """Sales orders: numbers, totals, payment fields (§3.6). The subscriber
    link has a two-path fallback (person, then quote person) — it is checked
    by the importer's unresolved-SO CSV, not re-derived here."""
    diffs: list[FieldDiff] = []
    _diff(
        diffs,
        "order_number",
        _norm_text(crm_row.get("order_number")),
        _norm_text(sub_row.get("order_number")),
    )
    _diff(
        diffs,
        "status",
        _norm_text(crm_row.get("status")),
        _norm_text(sub_row.get("status")),
    )
    for column in ("subtotal", "tax_total", "total", "amount_paid", "balance_due"):
        _diff(
            diffs,
            column,
            _norm_decimal(crm_row.get(column)),
            _norm_decimal(sub_row.get(column)),
        )
    for column in SO_PAYMENT_COLUMNS:
        crm_value = crm_row.get(column)
        sub_value = sub_row.get(column)
        if isinstance(crm_value, bool) or isinstance(sub_value, bool):
            _diff(diffs, column, bool(crm_value), bool(sub_value))
        else:
            _diff(diffs, column, _norm_text(crm_value), _norm_text(sub_value))
    _diff(
        diffs,
        "paid_at",
        _norm_ts(crm_row.get("paid_at")),
        _norm_ts(sub_row.get("paid_at")),
    )
    _diff(
        diffs,
        "quote_id",
        _norm_uuid(crm_row.get("quote_id")),
        _norm_uuid(sub_row.get("quote_id")),
    )
    return RowComparison(tuple(diffs))


def compare_project_fields(
    crm_row: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    subscriber_map: dict[str, str],
) -> RowComparison:
    """Projects: name, status, type, subscriber link, role UUIDs, dates,
    region (§3.6)."""
    diffs: list[FieldDiff] = []
    enrichments: list[FieldDiff] = []
    _diff(
        diffs, "name", _norm_text(crm_row.get("name")), _norm_text(sub_row.get("name"))
    )
    _diff(
        diffs,
        "status",
        _norm_text(crm_row.get("status")),
        _norm_text(sub_row.get("status")),
    )
    _diff(
        diffs,
        "project_type",
        _norm_text(crm_row.get("project_type")),
        _norm_text(sub_row.get("project_type")),
    )
    _diff(
        diffs,
        "region",
        _norm_text(crm_row.get("region")),
        _norm_text(sub_row.get("region")),
    )
    for column in PROJECT_ROLE_COLUMNS:
        _diff(
            diffs,
            column,
            _norm_uuid(crm_row.get(column)),
            _norm_uuid(sub_row.get(column)),
        )
    for column in PROJECT_DATE_COLUMNS:
        _diff(
            diffs, column, _norm_ts(crm_row.get(column)), _norm_ts(sub_row.get(column))
        )

    unresolved = None
    crm_subscriber_id = _uuid_or_none(crm_row.get("subscriber_id"))
    sub_subscriber_id = _norm_uuid(sub_row.get("subscriber_id"))
    if crm_subscriber_id is None:
        if sub_subscriber_id is not None:
            enrichments.append(FieldDiff("subscriber_id", None, sub_subscriber_id))
    else:
        mapped = subscriber_map.get(crm_subscriber_id)
        if mapped:
            _diff(diffs, "subscriber_id", mapped.lower(), sub_subscriber_id)
        else:
            unresolved = "unmapped_crm_subscriber"
    return RowComparison(tuple(diffs), tuple(enrichments), unresolved)


def compare_referral_fields(
    crm_row: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    party_map: dict[str, str],
    subscriber_map: dict[str, str],
) -> tuple[RowComparison, dict[str, str] | None]:
    """Referrals: status + reward fields + the collapsed referred link
    (§1.6/§3.6). Returns ``(comparison, referred-link disagreement row)``."""
    diffs: list[FieldDiff] = []
    enrichments: list[FieldDiff] = []
    _diff(
        diffs,
        "status",
        _norm_text(crm_row.get("status")),
        _norm_text(sub_row.get("status")),
    )
    _diff(
        diffs,
        "reward_amount",
        _norm_decimal(crm_row.get("reward_amount")),
        _norm_decimal(sub_row.get("reward_amount")),
    )
    for column in REFERRAL_REWARD_COLUMNS:
        _diff(
            diffs,
            column,
            _norm_text(crm_row.get(column)),
            _norm_text(sub_row.get(column)),
        )
    for column in ("reward_issued_at", "qualified_at"):
        _diff(
            diffs, column, _norm_ts(crm_row.get(column)), _norm_ts(sub_row.get(column))
        )

    unresolved = None
    expected_referrer = resolve_party_expectation(
        crm_row.get("referrer_person_id"), party_map
    )
    if expected_referrer:
        _diff(
            diffs,
            "referrer_subscriber_id",
            expected_referrer.lower(),
            _norm_uuid(sub_row.get("referrer_subscriber_id")),
        )
    else:
        unresolved = "unresolved_referrer_person"

    expected_referred, disagreement = resolve_referred_subscriber(
        referred_person_id=crm_row.get("referred_person_id"),
        crm_referred_subscriber_id=crm_row.get("referred_subscriber_id"),
        party_map=party_map,
        subscriber_map={key: value.lower() for key, value in subscriber_map.items()},
    )
    sub_referred = _norm_uuid(sub_row.get("referred_subscriber_id"))
    if expected_referred != sub_referred:
        if expected_referred is None and sub_referred is not None:
            enrichments.append(FieldDiff("referred_subscriber_id", None, sub_referred))
        else:
            diffs.append(
                FieldDiff("referred_subscriber_id", expected_referred, sub_referred)
            )
    if disagreement:
        disagreement = {"crm_id": str(crm_row.get("id")), **disagreement}
    return RowComparison(tuple(diffs), tuple(enrichments), unresolved), disagreement


def compare_referral_code_fields(
    crm_row: dict[str, Any],
    sub_row: dict[str, Any],
    *,
    party_map: dict[str, str],
) -> RowComparison:
    diffs: list[FieldDiff] = []
    _diff(
        diffs, "code", _norm_text(crm_row.get("code")), _norm_text(sub_row.get("code"))
    )
    unresolved = None
    expected = resolve_party_expectation(crm_row.get("person_id"), party_map)
    if expected:
        _diff(
            diffs,
            "subscriber_id",
            expected.lower(),
            _norm_uuid(sub_row.get("subscriber_id")),
        )
    else:
        unresolved = "unresolved_person"
    return RowComparison(tuple(diffs), (), unresolved)


def compare_children_counts(
    crm_counts: dict[str, Any], sub_counts: dict[str, Any], kinds: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    """Return ``(kind, crm_value, sub_value)`` per mismatched aggregate.

    Count kinds compare as ints; ``*_sum`` kinds as decimals (0 when absent).
    """
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


def so_sequence_findings(
    *,
    crm_numbers: list[int],
    sub_numbers: list[int],
    crm_next_value: int | None,
    sub_next_value: int | None,
) -> list[dict[str, Any]]:
    """§1.5/risk #10 numbering checks: set equality both ways, and sub's
    sequence row must clear CRM's next_value and every imported number."""
    findings: list[dict[str, Any]] = []
    crm_set, sub_set = set(crm_numbers), set(sub_numbers)
    for number in sorted(crm_set - sub_set):
        findings.append({"finding": "number_missing_in_sub", "value": number})
    for number in sorted(sub_set - crm_set):
        findings.append({"finding": "number_not_in_crm", "value": number})
    if crm_next_value is not None:
        if sub_next_value is None:
            findings.append(
                {"finding": "sequence_row_missing", "value": crm_next_value}
            )
        elif sub_next_value < crm_next_value:
            findings.append(
                {
                    "finding": "sequence_behind_crm",
                    "value": sub_next_value,
                    "expected_at_least": crm_next_value,
                }
            )
    if (
        sub_numbers
        and sub_next_value is not None
        and sub_next_value <= max(sub_numbers)
    ):
        findings.append(
            {
                "finding": "sequence_behind_max_number",
                "value": sub_next_value,
                "expected_at_least": max(sub_numbers) + 1,
            }
        )
    return findings


def triangle_findings(
    projects: list[dict[str, Any]],
    sales_orders: list[dict[str, Any]],
    quote_ids: set[str],
) -> list[dict[str, Any]]:
    """Quote -> SO -> project idempotency triangles on sub rows (§3.6):
    ``projects.metadata.quote_id``/``sales_order_id`` vs ``sales_orders``."""
    findings: list[dict[str, Any]] = []
    so_by_id = {str(so["id"]).lower(): so for so in sales_orders}
    for project in projects:
        metadata = _metadata_dict(project.get("metadata"))
        project_id = str(project.get("id"))
        quote_id = _norm_uuid(metadata.get("quote_id"))
        so_id = _norm_uuid(metadata.get("sales_order_id"))
        if quote_id and quote_id not in quote_ids:
            findings.append(
                {
                    "entity": "project",
                    "id": project_id,
                    "finding": "metadata_quote_missing",
                    "quote_id": quote_id,
                }
            )
        if so_id:
            so_row = so_by_id.get(so_id)
            if so_row is None:
                findings.append(
                    {
                        "entity": "project",
                        "id": project_id,
                        "finding": "metadata_sales_order_missing",
                        "sales_order_id": so_id,
                    }
                )
            elif quote_id and _norm_uuid(so_row.get("quote_id")) != quote_id:
                findings.append(
                    {
                        "entity": "project",
                        "id": project_id,
                        "finding": "quote_so_project_mismatch",
                        "quote_id": quote_id,
                        "sales_order_id": so_id,
                        "sales_order_quote_id": _norm_uuid(so_row.get("quote_id")),
                    }
                )
    for so in sales_orders:
        so_quote_id = _norm_uuid(so.get("quote_id"))
        if so_quote_id and so_quote_id not in quote_ids:
            findings.append(
                {
                    "entity": "sales_order",
                    "id": str(so.get("id")),
                    "finding": "quote_missing",
                    "quote_id": so_quote_id,
                }
            )
    return findings


def subscriber_sales_order_asymmetries(
    subscriber_links: list[dict[str, Any]],
    sales_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """§3.6 ``subscribers.sales_order_id`` symmetry: the linked SO should
    point back at the same subscriber."""
    so_subscriber = {
        str(so["id"]).lower(): _norm_uuid(so.get("subscriber_id"))
        for so in sales_orders
    }
    findings: list[dict[str, Any]] = []
    for link in subscriber_links:
        so_id = _norm_uuid(link.get("sales_order_id"))
        if not so_id:
            continue
        subscriber_id = _norm_uuid(link.get("id"))
        linked = so_subscriber.get(so_id)
        if so_id not in so_subscriber:
            findings.append(
                {
                    "subscriber_id": subscriber_id,
                    "sales_order_id": so_id,
                    "finding": "sales_order_missing",
                }
            )
        elif linked != subscriber_id:
            findings.append(
                {
                    "subscriber_id": subscriber_id,
                    "sales_order_id": so_id,
                    "finding": "sales_order_links_other_subscriber",
                    "sales_order_subscriber_id": linked,
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Loaders (monkeypatched in tests)
# ---------------------------------------------------------------------------

_CRM_TABLE_SQL: dict[str, str] = {
    "pipelines": "SELECT id::text, updated_at FROM crm_pipelines",
    "pipeline_stages": "SELECT id::text, updated_at FROM crm_pipeline_stages",
    "leads": """
        SELECT id::text, person_id::text, stage_id::text, status::text,
               estimated_value, lead_source, is_active, updated_at
        FROM crm_leads
    """,
    "quotes": """
        SELECT id::text, person_id::text, status::text, subtotal, tax_total,
               total, metadata::text, is_active, updated_at
        FROM crm_quotes
    """,
    "quote_line_items": "SELECT id::text, created_at FROM crm_quote_line_items",
    "sales_orders": """
        SELECT so.id::text, so.person_id::text, so.quote_id::text,
               so.order_number, so.status::text, so.payment_status::text,
               so.subtotal, so.tax_total, so.total, so.amount_paid,
               so.balance_due, so.deposit_required, so.deposit_paid,
               so.paid_at, so.is_active, so.updated_at,
               q.person_id::text AS quote_person_id
        FROM sales_orders so
        LEFT JOIN crm_quotes q ON q.id = so.quote_id
    """,
    "sales_order_lines": "SELECT id::text, updated_at FROM sales_order_lines",
    "project_templates": "SELECT id::text, updated_at FROM project_templates",
    "project_template_tasks": (
        "SELECT id::text, updated_at FROM project_template_tasks"
    ),
    "project_template_task_dependency": (
        "SELECT id::text FROM project_template_task_dependency"
    ),
    "projects": """
        SELECT id::text, name, status::text, project_type::text,
               subscriber_id::text, lead_id::text, created_by_person_id::text,
               owner_person_id::text, manager_person_id::text,
               project_manager_person_id::text,
               assistant_manager_person_id::text, start_at, due_at,
               completed_at, region, is_active, updated_at
        FROM projects
    """,
    "project_tasks": """
        SELECT id::text, project_id::text, assigned_to_person_id::text,
               created_by_person_id::text, updated_at
        FROM project_tasks
    """,
    "project_task_dependencies": "SELECT id::text FROM project_task_dependencies",
    "project_task_comments": (
        "SELECT id::text, author_person_id::text, created_at FROM project_task_comments"
    ),
    "project_comments": (
        "SELECT id::text, author_person_id::text, created_at FROM project_comments"
    ),
    "referral_codes": """
        SELECT id::text, person_id::text, code, is_active, created_at
        FROM referral_codes
    """,
    "referrals": """
        SELECT id::text, referrer_person_id::text, referred_person_id::text,
               referred_subscriber_id::text, status::text, reward_amount,
               reward_currency, reward_status::text, reward_issued_at,
               qualified_at, is_active, updated_at
        FROM referrals
    """,
    "work_links": (
        "SELECT id::text, source_type::text, target_type::text, created_at "
        "FROM work_links"
    ),
}

_SUB_TABLE_SQL: dict[str, str] = {
    "pipelines": "SELECT id::text FROM pipelines",
    "pipeline_stages": "SELECT id::text FROM pipeline_stages",
    "leads": """
        SELECT id::text, subscriber_id::text, stage_id::text, status,
               estimated_value, lead_source
        FROM leads
    """,
    "quotes": """
        SELECT id::text, subscriber_id::text, status, subtotal, tax_total,
               total, metadata::text
        FROM quotes
    """,
    "quote_line_items": "SELECT id::text FROM quote_line_items",
    "sales_orders": """
        SELECT id::text, subscriber_id::text, quote_id::text, order_number,
               status, payment_status, subtotal, tax_total, total,
               amount_paid, balance_due, deposit_required, deposit_paid,
               paid_at, metadata::text
        FROM sales_orders
    """,
    "sales_order_lines": "SELECT id::text FROM sales_order_lines",
    "project_templates": "SELECT id::text FROM project_templates",
    "project_template_tasks": "SELECT id::text FROM project_template_tasks",
    "project_template_task_dependency": (
        "SELECT id::text FROM project_template_task_dependency"
    ),
    "projects": """
        SELECT id::text, name, status, project_type, subscriber_id::text,
               created_by_person_id::text, owner_person_id::text,
               manager_person_id::text, project_manager_person_id::text,
               assistant_manager_person_id::text, start_at, due_at,
               completed_at, region, metadata::text
        FROM projects
    """,
    "project_tasks": "SELECT id::text FROM project_tasks",
    "project_task_dependencies": "SELECT id::text FROM project_task_dependencies",
    "project_task_comments": "SELECT id::text FROM project_task_comments",
    "project_comments": "SELECT id::text FROM project_comments",
    "referral_codes": (
        "SELECT id::text, subscriber_id::text, code FROM referral_codes"
    ),
    "referrals": """
        SELECT id::text, referrer_subscriber_id::text,
               referred_subscriber_id::text, status, reward_amount,
               reward_currency, reward_status, reward_issued_at, qualified_at
        FROM referrals
    """,
    "work_links": "SELECT id::text FROM work_links",
}


def _load_crm_tables(crm: Connection) -> dict[str, list[dict[str, Any]]]:
    return {name: _rows(crm, sql) for name, sql in _CRM_TABLE_SQL.items()}


def _load_sub_tables(sub: Connection) -> dict[str, list[dict[str, Any]]]:
    return {name: _rows(sub, sql) for name, sql in _SUB_TABLE_SQL.items()}


def _load_quote_line_aggregates(
    conn: Connection, table: str, parent: str
) -> dict[str, dict[str, Any]]:
    return {
        str(row["parent_id"]).lower(): {
            "lines": int(row["n"]),
            "lines_amount_sum": row["amount_sum"],
        }
        for row in _rows(
            conn,
            f"""
            SELECT {parent}::text AS parent_id, count(*) AS n,
                   COALESCE(sum(amount), 0) AS amount_sum
            FROM {table}
            GROUP BY {parent}
            """,  # noqa: S608
        )
    }


def _load_project_child_counts(conn: Connection) -> dict[str, dict[str, Any]]:
    # Table names are identical on both sides (§1.1 keeps CRM names).
    counts: dict[str, dict[str, Any]] = {}

    def _merge(rows: list[dict[str, Any]], kind: str) -> None:
        for row in rows:
            project_id = str(row["project_id"]).lower()
            counts.setdefault(project_id, {})[kind] = int(row["n"])

    _merge(
        _rows(
            conn,
            "SELECT project_id::text AS project_id, count(*) AS n "
            "FROM project_tasks GROUP BY project_id",
        ),
        "tasks",
    )
    _merge(
        _rows(
            conn,
            "SELECT project_id::text AS project_id, count(*) AS n "
            "FROM project_comments GROUP BY project_id",
        ),
        "comments",
    )
    _merge(
        _rows(
            conn,
            """
            SELECT t.project_id::text AS project_id, count(*) AS n
            FROM project_task_assignees a
            JOIN project_tasks t ON t.id = a.task_id
            GROUP BY t.project_id
            """,
        ),
        "task_assignees",
    )
    _merge(
        _rows(
            conn,
            """
            SELECT t.project_id::text AS project_id, count(*) AS n
            FROM project_task_dependencies d
            JOIN project_tasks t ON t.id = d.task_id
            GROUP BY t.project_id
            """,
        ),
        "task_dependencies",
    )
    _merge(
        _rows(
            conn,
            """
            SELECT t.project_id::text AS project_id, count(*) AS n
            FROM project_task_comments c
            JOIN project_tasks t ON t.id = c.task_id
            GROUP BY t.project_id
            """,
        ),
        "task_comments",
    )
    return counts


def _load_sub_sequence_next_value(sub: Connection) -> int | None:
    rows = _rows(
        sub,
        "SELECT next_value FROM document_sequences WHERE key = :key",
        {"key": SALES_ORDER_SEQUENCE_KEY},
    )
    return int(rows[0]["next_value"]) if rows else None


def _load_crm_sequence_next_value(crm: Connection) -> int | None:
    rows = _rows(
        crm,
        "SELECT next_value FROM document_sequences WHERE key = :key",
        {"key": SALES_ORDER_SEQUENCE_KEY},
    )
    return int(rows[0]["next_value"]) if rows else None


def _load_subscriber_sales_order_links(sub: Connection) -> list[dict[str, Any]]:
    return _rows(
        sub,
        """
        SELECT id::text, sales_order_id::text
        FROM subscribers
        WHERE sales_order_id IS NOT NULL
        """,
    )


# ---------------------------------------------------------------------------
# Drift run
# ---------------------------------------------------------------------------

PROJECT_CHILD_KINDS = (
    "tasks",
    "comments",
    "task_assignees",
    "task_dependencies",
    "task_comments",
)
LINE_CHILD_KINDS = ("lines", "lines_amount_sum")

# Verticals with a NOT NULL person->subscriber collapse: the importer skips
# inactive rows it cannot resolve, so their absence is policy, not drift.
_PARTY_PERSON_COLUMN = {
    "leads": "person_id",
    "quotes": "person_id",
    "sales_orders": "person_id",
    "referral_codes": "person_id",
    "referrals": "referrer_person_id",
}


def classify_missing_row(
    table: str,
    crm_row: dict[str, Any],
    *,
    party_map: dict[str, str],
) -> str:
    """``skipped_unresolved_inactive`` when the importer's policy explains the
    absence (inactive + unresolved party); gating ``crm_missing_in_sub``
    otherwise."""
    person_column = _PARTY_PERSON_COLUMN.get(table)
    if not person_column:
        return "crm_missing_in_sub"
    if bool(crm_row.get("is_active")):
        return "crm_missing_in_sub"
    resolvable = bool(resolve_party_expectation(crm_row.get(person_column), party_map))
    if table == "sales_orders" and not resolvable:
        # The importer's quote-person fallback (§3.5 step 5).
        resolvable = bool(
            resolve_party_expectation(crm_row.get("quote_person_id"), party_map)
        )
    return "crm_missing_in_sub" if resolvable else "skipped_unresolved_inactive"


def run_drift_check(
    *,
    sub: Connection,
    crm: Connection,
    window_minutes: int,
    staff_map: dict[str, str],
    party_map: dict[str, str],
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    now = now or datetime.now(UTC)

    crm_tables = _load_crm_tables(crm)
    sub_tables = _load_sub_tables(sub)
    for table in _SUB_TABLE_SQL:
        crm_tables.setdefault(table, [])
        sub_tables.setdefault(table, [])
    subscriber_map = _load_subscriber_map(sub)

    classes: dict[str, list[dict[str, Any]]] = {name: [] for name in ALL_CLASSES}
    in_flight: dict[str, list[str]] = {}
    table_counts: dict[str, dict[str, int]] = {}

    def _note_in_flight(table: str, row_id: str, finding: str) -> None:
        in_flight.setdefault(f"{table}:{row_id}", []).append(finding)

    sub_by_table: dict[str, dict[str, dict[str, Any]]] = {}
    for table, rows in sub_tables.items():
        sub_by_table[table] = {str(row["id"]).lower(): row for row in rows}

    # ---- generic id-set pass (missing / orphans) --------------------------
    for table, ts_column in ID_SET_TABLES:
        crm_rows = crm_tables.get(table, [])
        if table == "work_links":
            crm_rows = [
                row
                for row in crm_rows
                if work_link_is_phase3(row.get("source_type"), row.get("target_type"))
            ]
        sub_rows = sub_by_table.get(table, {})
        crm_ids = {str(row["id"]).lower() for row in crm_rows}
        table_counts[table] = {"crm": len(crm_rows), "sub": len(sub_rows)}
        for row in crm_rows:
            row_id = str(row["id"]).lower()
            if row_id in sub_rows:
                continue
            ts = _parse_datetime(row.get(ts_column)) if ts_column else None
            in_window = in_live_window(ts, now=now, window_minutes=window_minutes)
            target = classify_missing_row(table, row, party_map=party_map)
            classes[target].append(
                {
                    "table": table,
                    "crm_id": row_id,
                    "crm_ts": _format_datetime(ts),
                    "in_live_window": in_window,
                }
            )
            if in_window and target == "crm_missing_in_sub":
                _note_in_flight(table, row_id, "missing")
        for row_id in sub_rows:
            if row_id not in crm_ids:
                classes["sub_orphans"].append({"table": table, "sub_id": row_id})

    # ---- per-vertical field comparisons ------------------------------------
    def _window(table: str, crm_row: dict[str, Any], ts_column: str) -> bool:
        return in_live_window(
            _parse_datetime(crm_row.get(ts_column)),
            now=now,
            window_minutes=window_minutes,
        )

    def _emit(
        table: str,
        crm_row: dict[str, Any],
        comparison: RowComparison,
        *,
        ts_column: str = "updated_at",
    ) -> None:
        row_id = str(crm_row["id"]).lower()
        in_window = _window(table, crm_row, ts_column)
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

    for crm_row in crm_tables.get("leads", []):
        sub_row = sub_by_table["leads"].get(str(crm_row["id"]).lower())
        if sub_row:
            _emit(
                "leads",
                crm_row,
                compare_lead_fields(crm_row, sub_row, party_map=party_map),
            )

    for crm_row in crm_tables.get("quotes", []):
        sub_row = sub_by_table["quotes"].get(str(crm_row["id"]).lower())
        if sub_row:
            _emit(
                "quotes",
                crm_row,
                compare_quote_fields(crm_row, sub_row, party_map=party_map),
            )

    for crm_row in crm_tables.get("sales_orders", []):
        sub_row = sub_by_table["sales_orders"].get(str(crm_row["id"]).lower())
        if sub_row:
            _emit("sales_orders", crm_row, compare_sales_order_fields(crm_row, sub_row))

    for crm_row in crm_tables.get("projects", []):
        sub_row = sub_by_table["projects"].get(str(crm_row["id"]).lower())
        if sub_row:
            _emit(
                "projects",
                crm_row,
                compare_project_fields(crm_row, sub_row, subscriber_map=subscriber_map),
            )

    for crm_row in crm_tables.get("referral_codes", []):
        sub_row = sub_by_table["referral_codes"].get(str(crm_row["id"]).lower())
        if sub_row:
            _emit(
                "referral_codes",
                crm_row,
                compare_referral_code_fields(crm_row, sub_row, party_map=party_map),
                ts_column="created_at",
            )

    for crm_row in crm_tables.get("referrals", []):
        sub_row = sub_by_table["referrals"].get(str(crm_row["id"]).lower())
        if not sub_row:
            continue
        comparison, disagreement = compare_referral_fields(
            crm_row,
            sub_row,
            party_map=party_map,
            subscriber_map=subscriber_map,
        )
        _emit("referrals", crm_row, comparison)
        if disagreement:
            classes["referred_link_disagreements"].append(disagreement)

    # ---- children aggregates ------------------------------------------------
    crm_quote_lines = _load_quote_line_aggregates(
        crm, "crm_quote_line_items", "quote_id"
    )
    sub_quote_lines = _load_quote_line_aggregates(sub, "quote_line_items", "quote_id")
    crm_so_lines = _load_quote_line_aggregates(
        crm, "sales_order_lines", "sales_order_id"
    )
    sub_so_lines = _load_quote_line_aggregates(
        sub, "sales_order_lines", "sales_order_id"
    )
    crm_project_children = _load_project_child_counts(crm)
    sub_project_children = _load_project_child_counts(sub)

    def _children(
        table: str,
        crm_rows: list[dict[str, Any]],
        crm_agg: dict[str, dict[str, Any]],
        sub_agg: dict[str, dict[str, Any]],
        kinds: tuple[str, ...],
        ts_column: str,
    ) -> None:
        for crm_row in crm_rows:
            row_id = str(crm_row["id"]).lower()
            if row_id not in sub_by_table[table]:
                continue
            in_window = _window(table, crm_row, ts_column)
            for kind, crm_value, sub_value in compare_children_counts(
                crm_agg.get(row_id, {}), sub_agg.get(row_id, {}), kinds
            ):
                classes["children_count_mismatch"].append(
                    {
                        "table": table,
                        "crm_id": row_id,
                        "child": kind,
                        "crm_value": crm_value,
                        "sub_value": sub_value,
                        "in_live_window": in_window,
                    }
                )
                if in_window:
                    _note_in_flight(table, row_id, f"children:{kind}")

    _children(
        "quotes",
        crm_tables.get("quotes", []),
        crm_quote_lines,
        sub_quote_lines,
        LINE_CHILD_KINDS,
        "updated_at",
    )
    _children(
        "sales_orders",
        crm_tables.get("sales_orders", []),
        crm_so_lines,
        sub_so_lines,
        LINE_CHILD_KINDS,
        "updated_at",
    )
    _children(
        "projects",
        crm_tables.get("projects", []),
        crm_project_children,
        sub_project_children,
        PROJECT_CHILD_KINDS,
        "updated_at",
    )

    # ---- SO numbering / sequence continuity ---------------------------------
    crm_numbers = [
        number
        for number in (
            parse_order_number(row.get("order_number"))
            for row in crm_tables.get("sales_orders", [])
        )
        if number is not None
    ]
    sub_numbers = [
        number
        for number in (
            parse_order_number(row.get("order_number"))
            for row in sub_tables.get("sales_orders", [])
        )
        if number is not None
    ]
    classes["so_sequence"] = so_sequence_findings(
        crm_numbers=crm_numbers,
        sub_numbers=sub_numbers,
        crm_next_value=_load_crm_sequence_next_value(crm),
        sub_next_value=_load_sub_sequence_next_value(sub),
    )

    # ---- cross-checks (informational) ---------------------------------------
    classes["idempotency_triangle"] = triangle_findings(
        sub_tables.get("projects", []),
        sub_tables.get("sales_orders", []),
        set(sub_by_table["quotes"]),
    )
    classes["subscriber_sales_order_asymmetry"] = subscriber_sales_order_asymmetries(
        _load_subscriber_sales_order_links(sub),
        sub_tables.get("sales_orders", []),
    )

    # ---- unmapped staff (informational) --------------------------------------
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
        "so_sequence": len(classes["so_sequence"]),
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
    parser.add_argument("--out", default="phase3-drift-report")
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
        "--party-map",
        help=(
            "person_subscriber_map.csv from backfill_party_status.py; merged "
            "over the crm_person_id links stamped in sub metadata."
        ),
    )
    parser.add_argument(
        "--staff-map",
        help="staff_map.csv; staff UUIDs in it leave the unmapped_staff class.",
    )
    parser.add_argument("--limit-csv", type=int, default=50000)
    args = parser.parse_args()

    staff_map = _load_staff_map(args.staff_map)
    party_map_csv = _load_party_map_csv(args.party_map)
    out_dir = Path(args.out)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        sub.execute(text("SET TRANSACTION READ ONLY"))
        crm.execute(text("SET TRANSACTION READ ONLY"))
        try:
            party_map = merge_party_maps(party_map_csv, _load_party_map_from_sub(sub))
            summary, classes = run_drift_check(
                sub=sub,
                crm=crm,
                window_minutes=args.updated_within_minutes,
                staff_map=staff_map,
                party_map=party_map,
            )
        finally:
            sub.rollback()
            crm.rollback()

    for name, rows in classes.items():
        _write_csv(out_dir / f"{name}.csv", rows, max(1, args.limit_csv))

    exit_code = 0 if summary["drift_total"] == 0 else 1
    report = {
        **summary,
        "party_map": args.party_map,
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
