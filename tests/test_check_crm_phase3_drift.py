from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.engine import Connection

import scripts.migration.check_crm_phase3_drift as drift_mod
from scripts.migration.check_crm_phase3_drift import (
    GATING_CLASSES,
    INFO_CLASSES,
    FieldDiff,
    classify_missing_row,
    compare_children_counts,
    compare_lead_fields,
    compare_project_fields,
    compare_quote_fields,
    compare_referral_fields,
    compare_sales_order_fields,
    so_sequence_findings,
    subscriber_sales_order_asymmetries,
    triangle_findings,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=1)

PARTY_MAP = {"person-1": "sub-1", "person-2": "sub-2"}


# ---- leads -------------------------------------------------------------------


def _crm_lead(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "lead-1",
        "person_id": "person-1",
        "stage_id": "stage-1",
        "status": "qualified",
        "estimated_value": Decimal("1500.00"),
        "lead_source": "Referral",
        "is_active": True,
        "updated_at": OLD,
    }
    row.update(overrides)
    return row


def _sub_lead(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "lead-1",
        "subscriber_id": "sub-1",
        "stage_id": "STAGE-1",
        "status": "qualified",
        "estimated_value": Decimal("1500.0"),
        "lead_source": "Referral",
    }
    row.update(overrides)
    return row


def test_lead_identical_row_is_clean_across_normalizations() -> None:
    # Decimal trailing zeros and UUID case differences are not drift.
    comparison = compare_lead_fields(_crm_lead(), _sub_lead(), party_map=PARTY_MAP)

    assert comparison.diffs == ()
    assert comparison.unresolved_subscriber_reason is None


def test_lead_field_drift_per_spec_field_list() -> None:
    comparison = compare_lead_fields(
        _crm_lead(status="won", estimated_value=Decimal("2000.00")),
        _sub_lead(status="qualified", estimated_value=Decimal("1500.00")),
        party_map=PARTY_MAP,
    )

    assert {diff.field for diff in comparison.diffs} == {"status", "estimated_value"}


def test_lead_subscriber_link_checked_through_party_map() -> None:
    comparison = compare_lead_fields(
        _crm_lead(),
        _sub_lead(subscriber_id="sub-9"),
        party_map=PARTY_MAP,
    )

    assert FieldDiff("subscriber_id", "sub-1", "sub-9") in comparison.diffs


def test_lead_unresolved_person_counts_unresolved_not_drift() -> None:
    comparison = compare_lead_fields(
        _crm_lead(person_id="person-404"), _sub_lead(), party_map=PARTY_MAP
    )

    assert comparison.diffs == ()
    assert comparison.unresolved_subscriber_reason == "unresolved_person"


# ---- quotes ------------------------------------------------------------------


def _crm_quote(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "quote-1",
        "person_id": "person-1",
        "status": "accepted",
        "subtotal": Decimal("100.00"),
        "tax_total": Decimal("7.50"),
        "total": Decimal("107.50"),
        "metadata": '{"deposit": {"reference": "PAY-1", "paid": true}}',
        "is_active": True,
        "updated_at": OLD,
    }
    row.update(overrides)
    return row


def _sub_quote(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "quote-1",
        "subscriber_id": "sub-1",
        "status": "accepted",
        "subtotal": Decimal("100.00"),
        "tax_total": Decimal("7.50"),
        "total": Decimal("107.50"),
        "metadata": (
            '{"deposit": {"reference": "PAY-1", "paid": true},'
            ' "crm_person_id": "person-1",'
            ' "crm_import_source": "dotmac_crm_phase3"}'
        ),
    }
    row.update(overrides)
    return row


def test_quote_provenance_keys_are_not_drift() -> None:
    # The importer adds crm_person_id/crm_import_source; the §3.6 comparison
    # is on the deposit contract, not whole-metadata equality.
    comparison = compare_quote_fields(_crm_quote(), _sub_quote(), party_map=PARTY_MAP)

    assert comparison.diffs == ()


def test_quote_deposit_metadata_drift_gates() -> None:
    comparison = compare_quote_fields(
        _crm_quote(),
        _sub_quote(metadata='{"deposit": {"reference": "PAY-2", "paid": false}}'),
        party_map=PARTY_MAP,
    )

    assert [diff.field for diff in comparison.diffs] == ["metadata.deposit"]


def test_quote_deposit_only_in_sub_is_enrichment() -> None:
    comparison = compare_quote_fields(
        _crm_quote(metadata="{}"),
        _sub_quote(),
        party_map=PARTY_MAP,
    )

    assert comparison.diffs == ()
    assert [row.field for row in comparison.enrichments] == ["metadata.deposit"]


def test_quote_totals_drift() -> None:
    comparison = compare_quote_fields(
        _crm_quote(total=Decimal("200.00")), _sub_quote(), party_map=PARTY_MAP
    )

    assert [diff.field for diff in comparison.diffs] == ["total"]


# ---- sales orders ---------------------------------------------------------------


def _crm_so(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "so-1",
        "person_id": "person-1",
        "quote_id": "quote-1",
        "order_number": "SO-000042",
        "status": "confirmed",
        "payment_status": "partial",
        "subtotal": Decimal("100.00"),
        "tax_total": Decimal("0.00"),
        "total": Decimal("100.00"),
        "amount_paid": Decimal("50.00"),
        "balance_due": Decimal("50.00"),
        "deposit_required": True,
        "deposit_paid": True,
        "paid_at": None,
        "is_active": True,
        "updated_at": OLD,
    }
    row.update(overrides)
    return row


def _sub_so(**overrides: Any) -> dict[str, Any]:
    row = _crm_so()
    del row["person_id"]
    row["subscriber_id"] = "sub-1"
    row.update(overrides)
    return row


def test_sales_order_identical_is_clean() -> None:
    assert compare_sales_order_fields(_crm_so(), _sub_so()).diffs == ()


def test_sales_order_number_and_payment_fields_gate() -> None:
    comparison = compare_sales_order_fields(
        _crm_so(),
        _sub_so(
            order_number="SO-000043",
            payment_status="paid",
            deposit_paid=False,
            amount_paid=Decimal("100.00"),
        ),
    )

    assert {diff.field for diff in comparison.diffs} == {
        "order_number",
        "payment_status",
        "deposit_paid",
        "amount_paid",
    }


def test_sales_order_paid_at_normalizes_timestamps() -> None:
    comparison = compare_sales_order_fields(
        _crm_so(paid_at="2026-07-08T12:00:00Z"),
        _sub_so(paid_at=datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)),
    )

    assert comparison.diffs == ()


# ---- projects --------------------------------------------------------------------


def _crm_project(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "proj-1",
        "name": "Fiber install — Wuse II",
        "status": "active",
        "project_type": "fiber_optics_installation",
        "subscriber_id": "crm-sub-1",
        "created_by_person_id": "staff-1",
        "owner_person_id": None,
        "manager_person_id": None,
        "project_manager_person_id": "STAFF-2",
        "assistant_manager_person_id": None,
        "start_at": OLD,
        "due_at": None,
        "completed_at": None,
        "region": "Abuja",
        "is_active": True,
        "updated_at": OLD,
    }
    row.update(overrides)
    return row


def _sub_project(**overrides: Any) -> dict[str, Any]:
    row = _crm_project()
    row["subscriber_id"] = "sub-1"
    row["project_manager_person_id"] = "staff-2"
    row.update(overrides)
    return row


def test_project_identical_via_subscriber_map_is_clean() -> None:
    comparison = compare_project_fields(
        _crm_project(),
        _sub_project(),
        subscriber_map={"crm-sub-1": "sub-1"},
    )

    assert comparison.diffs == ()


def test_project_role_uuid_and_date_drift() -> None:
    comparison = compare_project_fields(
        _crm_project(),
        _sub_project(project_manager_person_id="staff-9", completed_at=NOW),
        subscriber_map={"crm-sub-1": "sub-1"},
    )

    assert {diff.field for diff in comparison.diffs} == {
        "project_manager_person_id",
        "completed_at",
    }


def test_project_subscriber_enrichment_when_crm_unlinked() -> None:
    comparison = compare_project_fields(
        _crm_project(subscriber_id=None),
        _sub_project(),
        subscriber_map={},
    )

    assert comparison.diffs == ()
    assert comparison.enrichments == (FieldDiff("subscriber_id", None, "sub-1"),)


def test_project_unmapped_crm_subscriber_counts_unresolved() -> None:
    comparison = compare_project_fields(
        _crm_project(), _sub_project(), subscriber_map={}
    )

    assert comparison.diffs == ()
    assert comparison.unresolved_subscriber_reason == "unmapped_crm_subscriber"


# ---- referrals -------------------------------------------------------------------


def _crm_referral(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "ref-1",
        "referrer_person_id": "person-1",
        "referred_person_id": "person-2",
        "referred_subscriber_id": None,
        "status": "qualified",
        "reward_amount": Decimal("5000.00"),
        "reward_currency": "NGN",
        "reward_status": "issued",
        "reward_issued_at": None,
        "qualified_at": OLD,
        "is_active": True,
        "updated_at": OLD,
    }
    row.update(overrides)
    return row


def _sub_referral(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "ref-1",
        "referrer_subscriber_id": "sub-1",
        "referred_subscriber_id": "sub-2",
        "status": "qualified",
        "reward_amount": Decimal("5000.00"),
        "reward_currency": "NGN",
        "reward_status": "issued",
        "reward_issued_at": None,
        "qualified_at": OLD,
    }
    row.update(overrides)
    return row


def test_referral_identical_collapse_is_clean() -> None:
    comparison, disagreement = compare_referral_fields(
        _crm_referral(),
        _sub_referral(),
        party_map=PARTY_MAP,
        subscriber_map={},
    )

    assert comparison.diffs == ()
    assert disagreement is None


def test_referral_reward_fields_gate() -> None:
    comparison, _ = compare_referral_fields(
        _crm_referral(reward_status="approved", reward_amount=Decimal("4000.00")),
        _sub_referral(),
        party_map=PARTY_MAP,
        subscriber_map={},
    )

    assert {diff.field for diff in comparison.diffs} == {
        "reward_status",
        "reward_amount",
    }


def test_referral_link_disagreement_reported_subscriber_path_wins() -> None:
    comparison, disagreement = compare_referral_fields(
        _crm_referral(referred_subscriber_id="crm-sub-9"),
        _sub_referral(referred_subscriber_id="sub-9"),
        party_map=PARTY_MAP,
        subscriber_map={"crm-sub-9": "SUB-9"},
    )

    # Sub row followed the winning subscriber path — no drift, one CSV row.
    assert comparison.diffs == ()
    assert disagreement is not None
    assert disagreement["crm_id"] == "ref-1"
    assert disagreement["via_person"] == "sub-2"
    assert disagreement["via_subscriber"] == "sub-9"


def test_referral_referred_mismatch_is_drift() -> None:
    comparison, _ = compare_referral_fields(
        _crm_referral(),
        _sub_referral(referred_subscriber_id="sub-9"),
        party_map=PARTY_MAP,
        subscriber_map={},
    )

    assert FieldDiff("referred_subscriber_id", "sub-2", "sub-9") in comparison.diffs


def test_referral_referred_enrichment_when_crm_unresolvable() -> None:
    comparison, _ = compare_referral_fields(
        _crm_referral(referred_person_id=None),
        _sub_referral(),
        party_map=PARTY_MAP,
        subscriber_map={},
    )

    assert comparison.diffs == ()
    assert comparison.enrichments == (
        FieldDiff("referred_subscriber_id", None, "sub-2"),
    )


# ---- children aggregates ------------------------------------------------------------


def test_children_counts_compare_ints_and_decimal_sums() -> None:
    mismatches = compare_children_counts(
        {"lines": 2, "lines_amount_sum": Decimal("100.00")},
        {"lines": 2, "lines_amount_sum": Decimal("100.0")},
        ("lines", "lines_amount_sum"),
    )
    assert mismatches == []

    mismatches = compare_children_counts(
        {"lines": 2, "lines_amount_sum": Decimal("100.00")},
        {"lines": 1, "lines_amount_sum": Decimal("60.00")},
        ("lines", "lines_amount_sum"),
    )
    assert mismatches == [
        ("lines", "2", "1"),
        ("lines_amount_sum", "100.00", "60.00"),
    ]


def test_children_counts_missing_keys_default_to_zero() -> None:
    assert compare_children_counts({}, {}, ("tasks", "lines_amount_sum")) == []
    assert compare_children_counts({"tasks": 3}, {}, ("tasks",)) == [
        ("tasks", "3", "0")
    ]


# ---- SO sequence gapless / continuity -------------------------------------------------


def test_so_sequence_clean() -> None:
    assert (
        so_sequence_findings(
            crm_numbers=[1, 2, 3],
            sub_numbers=[3, 2, 1],
            crm_next_value=4,
            sub_next_value=4,
        )
        == []
    )


def test_so_sequence_number_set_mismatch_both_directions() -> None:
    findings = so_sequence_findings(
        crm_numbers=[1, 2, 3],
        sub_numbers=[1, 3, 4],
        crm_next_value=4,
        sub_next_value=5,
    )

    assert {(f["finding"], f["value"]) for f in findings} == {
        ("number_missing_in_sub", 2),
        ("number_not_in_crm", 4),
    }


def test_so_sequence_row_missing_or_behind() -> None:
    assert so_sequence_findings(
        crm_numbers=[], sub_numbers=[], crm_next_value=7, sub_next_value=None
    ) == [{"finding": "sequence_row_missing", "value": 7}]

    findings = so_sequence_findings(
        crm_numbers=[1], sub_numbers=[1], crm_next_value=5, sub_next_value=2
    )
    assert findings == [
        {"finding": "sequence_behind_crm", "value": 2, "expected_at_least": 5}
    ]


def test_so_sequence_behind_max_imported_number() -> None:
    findings = so_sequence_findings(
        crm_numbers=[9], sub_numbers=[9], crm_next_value=None, sub_next_value=9
    )

    assert findings == [
        {"finding": "sequence_behind_max_number", "value": 9, "expected_at_least": 10}
    ]


# ---- cross-checks ----------------------------------------------------------------------


def test_triangle_findings_consistent_rows_are_clean() -> None:
    findings = triangle_findings(
        projects=[
            {
                "id": "proj-1",
                "metadata": '{"quote_id": "quote-1", "sales_order_id": "so-1"}',
            }
        ],
        sales_orders=[{"id": "so-1", "quote_id": "quote-1", "subscriber_id": "sub-1"}],
        quote_ids={"quote-1"},
    )

    assert findings == []


def test_triangle_findings_flag_missing_and_mismatched_links() -> None:
    findings = triangle_findings(
        projects=[
            {"id": "proj-1", "metadata": '{"quote_id": "quote-404"}'},
            {
                "id": "proj-2",
                "metadata": '{"quote_id": "quote-1", "sales_order_id": "so-2"}',
            },
        ],
        sales_orders=[
            {"id": "so-2", "quote_id": "quote-9", "subscriber_id": "sub-1"},
            {"id": "so-3", "quote_id": "quote-404", "subscriber_id": "sub-1"},
        ],
        quote_ids={"quote-1", "quote-9"},
    )

    assert {f["finding"] for f in findings} == {
        "metadata_quote_missing",
        "quote_so_project_mismatch",
        "quote_missing",
    }


def test_subscriber_sales_order_symmetry() -> None:
    findings = subscriber_sales_order_asymmetries(
        subscriber_links=[
            {"id": "sub-1", "sales_order_id": "so-1"},
            {"id": "sub-2", "sales_order_id": "so-1"},
            {"id": "sub-3", "sales_order_id": "so-404"},
        ],
        sales_orders=[{"id": "so-1", "quote_id": None, "subscriber_id": "sub-1"}],
    )

    assert [(f["subscriber_id"], f["finding"]) for f in findings] == [
        ("sub-2", "sales_order_links_other_subscriber"),
        ("sub-3", "sales_order_missing"),
    ]


# ---- importer-policy classification of missing rows -------------------------------------


def test_classify_missing_active_row_gates() -> None:
    assert (
        classify_missing_row(
            "leads", {"person_id": "person-404", "is_active": True}, party_map=PARTY_MAP
        )
        == "crm_missing_in_sub"
    )


def test_classify_missing_inactive_unresolved_is_policy_skip() -> None:
    assert (
        classify_missing_row(
            "leads",
            {"person_id": "person-404", "is_active": False},
            party_map=PARTY_MAP,
        )
        == "skipped_unresolved_inactive"
    )


def test_classify_missing_inactive_resolved_still_gates() -> None:
    assert (
        classify_missing_row(
            "leads", {"person_id": "person-1", "is_active": False}, party_map=PARTY_MAP
        )
        == "crm_missing_in_sub"
    )


def test_classify_missing_sales_order_uses_quote_person_fallback() -> None:
    row = {
        "person_id": "person-404",
        "quote_person_id": "person-1",
        "is_active": False,
    }

    assert (
        classify_missing_row("sales_orders", row, party_map=PARTY_MAP)
        == "crm_missing_in_sub"
    )


def test_classify_missing_non_party_table_always_gates() -> None:
    assert (
        classify_missing_row("projects", {"is_active": False}, party_map=PARTY_MAP)
        == "crm_missing_in_sub"
    )


# ---- run_drift_check classification + gating ----------------------------------------------


def _empty_tables() -> dict[str, list[dict[str, Any]]]:
    return {name: [] for name in drift_mod._SUB_TABLE_SQL}


def test_run_drift_check_classifies_and_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm_tables = _empty_tables()
    sub_tables = _empty_tables()

    crm_tables["leads"] = [
        _crm_lead(),  # clean
        _crm_lead(id="lead-missing", title="gone"),  # gating missing
        _crm_lead(id="lead-inflight", status="won", updated_at=NOW),  # in window
        _crm_lead(
            id="lead-policy-skip", person_id="person-404", is_active=False
        ),  # importer policy — non-gating
    ]
    sub_tables["leads"] = [
        _sub_lead(),
        _sub_lead(id="lead-inflight", status="qualified"),
        _sub_lead(id="lead-orphan"),  # gating orphan
    ]
    crm_tables["quotes"] = [_crm_quote()]
    sub_tables["quotes"] = [_sub_quote()]
    crm_tables["sales_orders"] = [_crm_so()]
    sub_tables["sales_orders"] = [_sub_so()]
    crm_tables["work_links"] = [
        {
            "id": "wl-deferred",
            "source_type": "project_task",
            "target_type": "work_order",
            "created_at": OLD,
        }
    ]

    monkeypatch.setattr(drift_mod, "_load_crm_tables", lambda conn: crm_tables)
    monkeypatch.setattr(drift_mod, "_load_sub_tables", lambda conn: sub_tables)
    monkeypatch.setattr(drift_mod, "_load_subscriber_map", lambda conn: {})
    monkeypatch.setattr(
        drift_mod, "_load_quote_line_aggregates", lambda conn, table, parent: {}
    )
    monkeypatch.setattr(drift_mod, "_load_project_child_counts", lambda conn: {})
    monkeypatch.setattr(drift_mod, "_load_crm_sequence_next_value", lambda conn: 43)
    monkeypatch.setattr(drift_mod, "_load_sub_sequence_next_value", lambda conn: 43)
    monkeypatch.setattr(
        drift_mod, "_load_subscriber_sales_order_links", lambda conn: []
    )

    summary, classes = drift_mod.run_drift_check(
        sub=cast(Connection, None),
        crm=cast(Connection, None),
        window_minutes=30,
        staff_map={},
        party_map=PARTY_MAP,
        now=NOW,
    )

    # lead-missing gates; the deferred work_link is filtered out entirely.
    assert summary["classes"]["crm_missing_in_sub"] == {"rows": 1, "drift": 1}
    assert classes["crm_missing_in_sub"][0]["crm_id"] == "lead-missing"
    assert summary["table_counts"]["work_links"] == {"crm": 0, "sub": 0}

    # Policy skip is informational.
    assert summary["classes"]["skipped_unresolved_inactive"] == {"rows": 1, "drift": 0}
    assert classes["skipped_unresolved_inactive"][0]["crm_id"] == "lead-policy-skip"

    # Orphan gates.
    assert summary["classes"]["sub_orphans"] == {"rows": 1, "drift": 1}
    assert classes["sub_orphans"][0]["sub_id"] == "lead-orphan"

    # In-window status drift reported but not gating.
    in_window_rows = [
        row for row in classes["field_drift"] if row["crm_id"] == "lead-inflight"
    ]
    assert in_window_rows and all(row["in_live_window"] for row in in_window_rows)
    assert summary["classes"]["field_drift"]["drift"] == 0
    assert any(
        row["crm_id"] == "lead-inflight" for row in classes["expected_in_flight"]
    )

    # Quote/SO rows are clean; sequence is continuous.
    assert summary["classes"]["so_sequence"] == {"rows": 0, "drift": 0}

    assert summary["drift_total"] == 2


def test_gating_and_info_classes_are_disjoint_and_complete() -> None:
    assert not set(GATING_CLASSES) & set(INFO_CLASSES)
    assert set(drift_mod.ALL_CLASSES) == set(GATING_CLASSES) | set(INFO_CLASSES)
