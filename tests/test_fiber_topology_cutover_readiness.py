from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.network.fiber_topology_cutover_readiness import (
    FIBER_TOPOLOGY_CUTOVER_POLICY,
    GLOBAL_COHORT_NAME,
    FiberTopologyCutoverEvidence,
    FiberTopologyCutoverReadinessError,
    evaluate_fiber_cutover_readiness,
    reconcile_fiber_cutover_readiness,
)


def _passing_evidence() -> FiberTopologyCutoverEvidence:
    return FiberTopologyCutoverEvidence(
        cohort_name=GLOBAL_COHORT_NAME,
        identity_report_sha256="1" * 64,
        identity_total=2,
        identity_exact_current=2,
        identity_terminal_current=2,
        identity_blocker_codes=(),
        connectivity_report_sha256="2" * 64,
        connectivity_total=3,
        connectivity_exact_current=3,
        connectivity_terminal_current=3,
        connectivity_blocker_codes=(),
        topology_report_sha256="3" * 64,
        topology_blocker_codes=(),
        customer_trace_total=4,
        customer_trace_evaluated=4,
        customer_trace_complete=4,
        field_worklist_report_sha256="4" * 64,
        required_field_total=5,
        required_field_current_agreement=5,
        required_field_blocker_codes=(),
        represented_critical_field_scopes=(
            "pop_olt",
            "feeder_trunk",
            "cabinet",
            "splitter",
            "customer_bearing_endpoint",
            "changed_or_conflicting_source",
        ),
        dormant_low_risk_total=0,
        dormant_sample_selected=0,
        dormant_sample_current_agreement=0,
        dormant_sample_discrepancies=0,
    )


def _gate(report, code: str) -> dict[str, object]:
    return next(gate for gate in report.gates if gate["code"] == code)


def test_exact_versioned_policy_can_pass_only_as_cutover_review_evidence():
    report = evaluate_fiber_cutover_readiness(_passing_evidence())
    replay = evaluate_fiber_cutover_readiness(_passing_evidence())

    assert FIBER_TOPOLOGY_CUTOVER_POLICY.exact_coverage_bps == 10_000
    assert FIBER_TOPOLOGY_CUTOVER_POLICY.required_field_verification_bps == 10_000
    assert FIBER_TOPOLOGY_CUTOVER_POLICY.dormant_low_risk_sample_bps == 2_000
    assert FIBER_TOPOLOGY_CUTOVER_POLICY.dormant_low_risk_minimum_sample == 25
    assert FIBER_TOPOLOGY_CUTOVER_POLICY.dormant_discrepancy_expansion_bps == 200
    assert FIBER_TOPOLOGY_CUTOVER_POLICY.dormant_low_risk_classifier_owner is None
    assert report.ready_for_cutover_review is True
    assert report.readiness_blocker_codes == ()
    assert report.dormant_sample_required_count == 0
    assert report.report_sha256 == replay.report_sha256
    assert report.cohort_sha256 == replay.cohort_sha256
    assert len(report.policy_sha256) == 64


@pytest.mark.parametrize(
    ("changes", "blocked_gate"),
    [
        ({"identity_exact_current": 1}, "identity_exact_current_coverage"),
        (
            {"identity_terminal_current": 1},
            "identity_review_result_provenance_coverage",
        ),
        (
            {"identity_blocker_codes": ("identity:pending_review",)},
            "identity_owner_blockers_zero",
        ),
        (
            {"connectivity_exact_current": 2},
            "connectivity_exact_current_coverage",
        ),
        (
            {"connectivity_terminal_current": 2},
            "connectivity_review_result_provenance_coverage",
        ),
        (
            {"connectivity_blocker_codes": ("connectivity:drift",)},
            "connectivity_owner_blockers_zero",
        ),
        (
            {"topology_blocker_codes": ("active_olt_without_pop_site",)},
            "canonical_topology_blockers_zero",
        ),
        (
            {"customer_trace_evaluated": 3, "customer_trace_complete": 3},
            "customer_bearing_paths_exhaustively_evaluated",
        ),
        (
            {"customer_trace_complete": 3},
            "customer_bearing_paths_traceable",
        ),
        (
            {"required_field_current_agreement": 4},
            "required_field_rows_current_agreement",
        ),
        (
            {"required_field_blocker_codes": ("field:unobserved",)},
            "required_field_blockers_zero",
        ),
    ],
)
def test_zero_tolerance_gate_blocks_any_incomplete_or_problem_evidence(
    changes, blocked_gate
):
    report = evaluate_fiber_cutover_readiness(replace(_passing_evidence(), **changes))

    assert report.ready_for_cutover_review is False
    assert blocked_gate in report.readiness_blocker_codes


def test_empty_cohorts_fail_closed_instead_of_vacuously_passing():
    evidence = replace(
        _passing_evidence(),
        identity_total=0,
        identity_exact_current=0,
        identity_terminal_current=0,
        connectivity_total=0,
        connectivity_exact_current=0,
        connectivity_terminal_current=0,
        customer_trace_total=0,
        customer_trace_evaluated=0,
        customer_trace_complete=0,
        required_field_total=0,
        required_field_current_agreement=0,
        dormant_low_risk_total=0,
        dormant_sample_selected=0,
        dormant_sample_current_agreement=0,
    )

    report = evaluate_fiber_cutover_readiness(evidence)

    assert report.ready_for_cutover_review is False
    assert _gate(report, "identity_exact_current_coverage")["ready"] is False
    assert _gate(report, "connectivity_exact_current_coverage")["ready"] is False
    assert (
        _gate(report, "customer_bearing_paths_exhaustively_evaluated")["ready"] is False
    )


def test_missing_critical_field_contract_blocks_without_inference():
    report = evaluate_fiber_cutover_readiness(
        replace(
            _passing_evidence(),
            represented_critical_field_scopes=("cabinet", "feeder_trunk"),
        )
    )

    gate = _gate(report, "critical_field_scope_contract_complete")
    assert gate["ready"] is False
    assert gate["missing_scopes"] == [
        "changed_or_conflicting_source",
        "customer_bearing_endpoint",
        "pop_olt",
        "splitter",
    ]


def test_dormant_sample_uses_twenty_percent_with_twenty_five_row_minimum():
    small = evaluate_fiber_cutover_readiness(
        replace(
            _passing_evidence(),
            dormant_low_risk_total=100,
            dormant_sample_selected=25,
            dormant_sample_current_agreement=25,
        )
    )
    large = evaluate_fiber_cutover_readiness(
        replace(
            _passing_evidence(),
            dormant_low_risk_total=200,
            dormant_sample_selected=40,
            dormant_sample_current_agreement=40,
        )
    )
    short = evaluate_fiber_cutover_readiness(
        replace(
            _passing_evidence(),
            dormant_low_risk_total=200,
            dormant_sample_selected=39,
            dormant_sample_current_agreement=39,
        )
    )

    assert small.dormant_sample_required_count == 25
    assert large.dormant_sample_required_count == 40
    assert _gate(large, "dormant_low_risk_sample_selection_complete")["ready"] is True
    assert "dormant_low_risk_classification_authoritative" in (
        large.readiness_blocker_codes
    )
    assert short.ready_for_cutover_review is False
    assert "dormant_low_risk_sample_selection_complete" in (
        short.readiness_blocker_codes
    )


def test_discrepancy_above_two_percent_expands_class_and_any_discrepancy_blocks():
    exactly_two_percent = evaluate_fiber_cutover_readiness(
        replace(
            _passing_evidence(),
            dormant_low_risk_total=250,
            dormant_sample_selected=50,
            dormant_sample_current_agreement=49,
            dormant_sample_discrepancies=1,
        )
    )
    above_two_percent = evaluate_fiber_cutover_readiness(
        replace(
            _passing_evidence(),
            dormant_low_risk_total=200,
            dormant_sample_selected=40,
            dormant_sample_current_agreement=39,
            dormant_sample_discrepancies=1,
        )
    )

    assert exactly_two_percent.dormant_asset_class_expansion_required is False
    assert exactly_two_percent.ready_for_cutover_review is False
    assert above_two_percent.dormant_asset_class_expansion_required is True
    assert above_two_percent.dormant_sample_required_count == 200
    assert "dormant_low_risk_sample_selection_complete" in (
        above_two_percent.readiness_blocker_codes
    )
    assert "known_dormant_sample_discrepancies_zero" in (
        above_two_percent.readiness_blocker_codes
    )


def test_inconsistent_evidence_counts_fail_closed():
    with pytest.raises(
        FiberTopologyCutoverReadinessError,
        match="numerator exceeds",
    ):
        evaluate_fiber_cutover_readiness(
            replace(_passing_evidence(), identity_exact_current=3)
        )

    with pytest.raises(
        FiberTopologyCutoverReadinessError,
        match="invalid exact evidence SHA-256",
    ):
        evaluate_fiber_cutover_readiness(
            replace(_passing_evidence(), identity_report_sha256="not-a-hash")
        )


def test_database_reconciliation_is_read_only_and_currently_reports_scope_gaps(
    db_session,
):
    report = reconcile_fiber_cutover_readiness(db_session)

    assert report.ready_for_cutover_review is False
    assert "critical_field_scope_contract_complete" in report.readiness_blocker_codes
    assert len(report.report_sha256) == 64
    assert len(report.cohort_sha256) == 64
