import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.migration.import_crm_phase3 import (
    STEP_ORDER,
    TABLE_SPECS,
    PartyResolution,
    _load_party_map_csv,
    build_upsert_sql,
    merge_party_maps,
    parse_order_number,
    plan_lead_open_unique_conflicts,
    plan_referral_referred_unique_conflicts,
    provenance_metadata,
    rekey_ticket_id,
    resolve_party_subscriber,
    resolve_referred_subscriber,
    resolve_sales_order_subscriber,
    seed_sequence_next_value,
    watermark_key,
    work_link_is_phase3,
    write_state_keys,
)
from scripts.migration.import_crm_tickets_phase1 import _state_watermark

PARTY_MAP = {"person-1": "sub-1", "person-2": "sub-2"}


# ---- §3.5 step order ---------------------------------------------------------


def test_step_order_is_fk_driven_per_spec() -> None:
    assert STEP_ORDER == (
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


def test_step_order_fk_dependencies_hold() -> None:
    index = {step: i for i, step in enumerate(STEP_ORDER)}
    # Leads before everything that FKs them; the ticket lead_id backfill
    # rides §3.5 step 3, straight after the leads import.
    assert index["pipelines"] < index["pipeline_stages"] < index["leads"]
    assert index["leads"] + 1 == index["support_ticket_lead_ids"]
    assert index["leads"] < index["quotes"] < index["quote_line_items"]
    # Sequence seeded before any sales order rows exist (§1.5).
    assert index["sales_order_sequence"] < index["sales_orders"]
    assert index["sales_orders"] < index["sales_order_lines"]
    assert index["sales_orders"] < index["subscriber_sales_order_ids"]
    # Projects family: templates -> projects -> tasks -> children.
    assert (
        index["project_templates"]
        < index["project_template_tasks"]
        < index["project_template_task_dependency"]
        < index["projects"]
        < index["project_tasks"]
        < index["project_task_assignees"]
    )
    assert index["project_tasks"] < index["project_task_dependencies"]
    assert index["project_tasks"] < index["project_task_comments"]
    assert index["projects"] < index["project_comments"]
    # Referrals need leads (referred_lead_id) and codes first.
    assert index["leads"] < index["referral_codes"] < index["referrals"]
    assert index["referrals"] < index["work_links"]


# ---- party map artifact --------------------------------------------------------


def _write_party_map(path: Path, rows: list[dict[str, str]]) -> Path:
    fieldnames = ["crm_person_id", "subscriber_id", "resolution", "sources"]
    lines = [",".join(fieldnames)]
    for row in rows:
        lines.append(",".join(row.get(name, "") for name in fieldnames))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_party_map_csv_lowercases_and_skips_blank(tmp_path: Path) -> None:
    csv_path = _write_party_map(
        tmp_path / "person_subscriber_map.csv",
        [
            {
                "crm_person_id": "PERSON-1",
                "subscriber_id": "SUB-1",
                "resolution": "crm_person_id",
            },
            {"crm_person_id": "person-2", "subscriber_id": "", "resolution": ""},
        ],
    )

    assert _load_party_map_csv(str(csv_path)) == {"person-1": "sub-1"}


def test_load_party_map_csv_rejects_conflicts(tmp_path: Path) -> None:
    csv_path = _write_party_map(
        tmp_path / "person_subscriber_map.csv",
        [
            {"crm_person_id": "person-1", "subscriber_id": "sub-1"},
            {"crm_person_id": "person-1", "subscriber_id": "sub-2"},
        ],
    )

    with pytest.raises(SystemExit):
        _load_party_map_csv(str(csv_path))


def test_load_party_map_csv_without_path_is_empty() -> None:
    assert _load_party_map_csv(None) == {}


def test_merge_party_maps_csv_wins() -> None:
    merged = merge_party_maps(
        {"person-1": "sub-csv"},
        {"person-1": "sub-db", "person-2": "sub-2"},
    )

    assert merged == {"person-1": "sub-csv", "person-2": "sub-2"}


# ---- party resolution / blockers policy -----------------------------------------


def test_resolve_party_active_hit() -> None:
    resolution = resolve_party_subscriber(
        "PERSON-1", is_active=True, party_map=PARTY_MAP
    )

    assert resolution == PartyResolution("sub-1", "resolved")


def test_resolve_party_active_miss_blocks() -> None:
    resolution = resolve_party_subscriber(
        "person-404", is_active=True, party_map=PARTY_MAP
    )

    assert resolution == PartyResolution(None, "block", "unresolved_person")


def test_resolve_party_inactive_miss_skips() -> None:
    resolution = resolve_party_subscriber(
        "person-404", is_active=False, party_map=PARTY_MAP
    )

    assert resolution == PartyResolution(None, "skip", "unresolved_person_inactive")


def test_resolve_party_missing_person_blocks_even_inactive() -> None:
    resolution = resolve_party_subscriber(None, is_active=False, party_map=PARTY_MAP)

    assert resolution == PartyResolution(None, "block", "missing_person_id")


def test_resolve_sales_order_prefers_person_then_quote_person() -> None:
    primary, method = resolve_sales_order_subscriber(
        person_id="person-1",
        quote_person_id="person-2",
        is_active=True,
        party_map=PARTY_MAP,
    )
    assert (primary.subscriber_id, method) == ("sub-1", "person")

    fallback, method = resolve_sales_order_subscriber(
        person_id="person-404",
        quote_person_id="person-2",
        is_active=True,
        party_map=PARTY_MAP,
    )
    assert (fallback.subscriber_id, method) == ("sub-2", "quote_person")

    unresolved, method = resolve_sales_order_subscriber(
        person_id="person-404",
        quote_person_id="person-405",
        is_active=True,
        party_map=PARTY_MAP,
    )
    assert unresolved.action == "block"
    assert method is None


# ---- referred-link collapse -----------------------------------------------------


def test_referred_subscriber_path_wins_and_reports_disagreement() -> None:
    resolved, disagreement = resolve_referred_subscriber(
        referred_person_id="person-1",
        crm_referred_subscriber_id="crm-sub-9",
        party_map=PARTY_MAP,
        subscriber_map={"crm-sub-9": "sub-9"},
    )

    assert resolved == "sub-9"
    assert disagreement == {
        "crm_referred_person_id": "person-1",
        "crm_referred_subscriber_id": "crm-sub-9",
        "via_person": "sub-1",
        "via_subscriber": "sub-9",
    }


def test_referred_person_path_used_when_subscriber_path_empty() -> None:
    resolved, disagreement = resolve_referred_subscriber(
        referred_person_id="person-1",
        crm_referred_subscriber_id=None,
        party_map=PARTY_MAP,
        subscriber_map={},
    )

    assert resolved == "sub-1"
    assert disagreement is None


def test_referred_agreeing_paths_are_clean() -> None:
    resolved, disagreement = resolve_referred_subscriber(
        referred_person_id="person-1",
        crm_referred_subscriber_id="crm-sub-1",
        party_map=PARTY_MAP,
        subscriber_map={"crm-sub-1": "sub-1"},
    )

    assert resolved == "sub-1"
    assert disagreement is None


def test_referred_unresolvable_is_none() -> None:
    resolved, disagreement = resolve_referred_subscriber(
        referred_person_id="person-404",
        crm_referred_subscriber_id="crm-sub-404",
        party_map=PARTY_MAP,
        subscriber_map={},
    )

    assert resolved is None
    assert disagreement is None


# ---- ticket re-key (§1.2 / risk #14) --------------------------------------------


def test_rekey_ticket_id_applies_phase1_map() -> None:
    assert rekey_ticket_id("CRM-T1", {"crm-t1": "sub-t1"}) == ("sub-t1", False)


def test_rekey_ticket_id_dangling_nulls_out() -> None:
    assert rekey_ticket_id("crm-gone", {"crm-t1": "sub-t1"}) == (None, True)


def test_rekey_ticket_id_none_passthrough() -> None:
    assert rekey_ticket_id(None, {}) == (None, False)


# ---- partial-unique pre-flights ---------------------------------------------------


def _lead(lead_id: str, **overrides: object) -> dict[str, object]:
    lead: dict[str, object] = {
        "id": lead_id,
        "subscriber_id": "sub-1",
        "pipeline_id": "pipe-1",
        "status": "new",
        "is_active": True,
    }
    lead.update(overrides)
    return lead


def test_lead_open_unique_conflict_on_collapsed_subscriber() -> None:
    conflicts = plan_lead_open_unique_conflicts(
        [_lead("l-1"), _lead("l-2"), _lead("l-3", subscriber_id="sub-2")]
    )

    assert conflicts == [
        {"subscriber_id": "sub-1", "pipeline_id": "pipe-1", "lead_ids": "l-1;l-2"}
    ]


def test_lead_open_unique_ignores_closed_inactive_and_null_pipeline_groups() -> None:
    assert (
        plan_lead_open_unique_conflicts(
            [
                _lead("l-1", status="won"),
                _lead("l-2", is_active=False),
                _lead("l-3"),
                # NULL pipeline coalesces to the sentinel — its own group.
                _lead("l-4", pipeline_id=None),
                _lead("l-5", pipeline_id=None, status="lost"),
            ]
        )
        == []
    )
    conflicts = plan_lead_open_unique_conflicts(
        [_lead("l-4", pipeline_id=None), _lead("l-6", pipeline_id=None)]
    )
    assert len(conflicts) == 1
    assert conflicts[0]["lead_ids"] == "l-4;l-6"


def test_referral_referred_unique_conflicts() -> None:
    conflicts = plan_referral_referred_unique_conflicts(
        [
            {"id": "r-1", "referred_subscriber_id": "sub-1", "is_active": True},
            {"id": "r-2", "referred_subscriber_id": "sub-1", "is_active": True},
            {"id": "r-3", "referred_subscriber_id": "sub-1", "is_active": False},
            {"id": "r-4", "referred_subscriber_id": None, "is_active": True},
        ]
    )

    assert conflicts == [{"referred_subscriber_id": "sub-1", "referral_ids": "r-1;r-2"}]


# ---- work links (§3.5 step 7) -----------------------------------------------------


def test_work_link_phase3_gate() -> None:
    assert work_link_is_phase3("project", "sales_order")
    assert work_link_is_phase3("project_task", "lead")
    # Work-order rows wait for Phase 2; ticket rows ride with them.
    assert not work_link_is_phase3("project_task", "work_order")
    assert not work_link_is_phase3("ticket", "project")
    assert not work_link_is_phase3(None, "project")


# ---- SO-%06d sequence continuity (§1.5, risk #10) -----------------------------------


def test_parse_order_number() -> None:
    assert parse_order_number("SO-000123") == 123
    assert parse_order_number("SO-1") == 1
    assert parse_order_number("INV-000123") is None
    assert parse_order_number("SO-12x") is None
    assert parse_order_number(None) is None


def test_seed_sequence_continues_crm_sequence() -> None:
    assert (
        seed_sequence_next_value(
            sub_next_value=None, crm_next_value=57, max_order_number=56
        )
        == 57
    )


def test_seed_sequence_never_decreases_and_clears_max_number() -> None:
    # Existing sub value wins when higher.
    assert (
        seed_sequence_next_value(
            sub_next_value=90, crm_next_value=57, max_order_number=56
        )
        == 90
    )
    # A stale CRM sequence row still clears the highest imported number.
    assert (
        seed_sequence_next_value(
            sub_next_value=None, crm_next_value=3, max_order_number=88
        )
        == 89
    )
    assert (
        seed_sequence_next_value(
            sub_next_value=None, crm_next_value=None, max_order_number=None
        )
        == 1
    )


# ---- provenance metadata -----------------------------------------------------------


def test_provenance_metadata_merges_and_stamps_source() -> None:
    metadata = provenance_metadata(
        json.dumps({"source": "portal_self_serve"}),
        {"crm_person_id": "person-1", "skipped_none": None},
    )

    assert metadata == {
        "source": "portal_self_serve",
        "crm_person_id": "person-1",
        "crm_import_source": "dotmac_crm_phase3",
    }


def test_provenance_metadata_wraps_non_dict_raw() -> None:
    metadata = provenance_metadata(json.dumps(["not", "a", "dict"]), {})

    assert metadata["crm_metadata_raw"] == ["not", "a", "dict"]
    assert metadata["crm_import_source"] == "dotmac_crm_phase3"


# ---- generic upsert SQL --------------------------------------------------------------


def test_build_upsert_sql_casts_and_updates() -> None:
    sql = build_upsert_sql(TABLE_SPECS["leads"])

    assert "INSERT INTO leads" in sql
    assert "CAST(:id AS uuid)" in sql
    assert "CAST(:subscriber_id AS uuid)" in sql
    assert "CAST(:metadata AS json)" in sql
    assert "ON CONFLICT (id) DO UPDATE SET" in sql
    assert "subscriber_id = EXCLUDED.subscriber_id" in sql
    # PK and created_at are immutable across re-runs.
    assert "id = EXCLUDED.id" not in sql
    assert "created_at = EXCLUDED.created_at" not in sql


def test_build_upsert_sql_do_nothing_for_immutable_children() -> None:
    sql = build_upsert_sql(TABLE_SPECS["project_task_comments"])

    assert sql.rstrip().endswith("DO NOTHING")


def test_task_assignees_conflict_on_composite_pk() -> None:
    sql = build_upsert_sql(TABLE_SPECS["project_task_assignees"])

    assert "ON CONFLICT (task_id, person_id) DO NOTHING" in sql


def test_project_tasks_spec_defers_parent_link_to_phase_two() -> None:
    # Two-phase apply: parent_task_id is re-linked by an UPDATE after all
    # rows exist, so the insert spec must not carry it.
    assert "parent_task_id" not in TABLE_SPECS["project_tasks"].columns
    # work_order_id stays a plain UUID column but does import (§1.2).
    assert "work_order_id" in TABLE_SPECS["project_tasks"].columns


def test_table_specs_exist_for_every_import_table() -> None:
    expected = {
        "pipelines",
        "pipeline_stages",
        "leads",
        "quotes",
        "quote_line_items",
        "sales_orders",
        "sales_order_lines",
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
    }

    assert set(TABLE_SPECS) == expected


# ---- state-file watermarks -------------------------------------------------------------


def test_write_state_keys_merges_per_table_watermarks(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    write_state_keys(
        str(state), {watermark_key("crm_leads"): "2026-07-08T00:00:00+00:00"}
    )
    write_state_keys(
        str(state),
        {
            watermark_key("crm_quotes"): "2026-07-09T00:00:00+00:00",
            watermark_key("crm_projects"): None,  # None preserves/skips
        },
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload[watermark_key("crm_leads")] == "2026-07-08T00:00:00+00:00"
    assert payload[watermark_key("crm_quotes")] == "2026-07-09T00:00:00+00:00"
    assert watermark_key("crm_projects") not in payload
    assert _state_watermark(str(state), watermark_key("crm_leads"), 0) == datetime(
        2026, 7, 8, tzinfo=UTC
    )


def test_write_state_keys_without_values_is_noop(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    write_state_keys(str(state), {watermark_key("crm_leads"): None})

    assert not state.exists()
