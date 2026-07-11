"""Pure-logic tests for the maps §B vendor-route drift checker.

Covers the table-driven field comparison (scalar drift, the collapsed
installation_projects.subscriber_id link, route_geom EWKT drift), the
missing-row classifier (native_project_absent), the children-count comparison
(count + amount sum), and the gating/info class split.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from scripts.migration.check_crm_vendor_routes_drift import (
    ALL_CLASSES,
    GATING_CLASSES,
    INFO_CLASSES,
    classify_missing_row,
    compare_children_counts,
    compare_fields,
)

SUBSCRIBER_MAP = {"crm-sub-1": "sub-1", "crm-sub-2": "sub-2"}


# ---------------------------------------------------------------------------
# Class split
# ---------------------------------------------------------------------------


def test_gating_and_info_classes_are_disjoint() -> None:
    assert set(GATING_CLASSES).isdisjoint(INFO_CLASSES)
    assert set(ALL_CLASSES) == set(GATING_CLASSES) | set(INFO_CLASSES)


# ---------------------------------------------------------------------------
# Field comparison
# ---------------------------------------------------------------------------


def test_vendor_field_drift_detected() -> None:
    crm = {"id": "v1", "name": "Fibre Co", "code": "FC", "contact_email": "a@x",
           "is_active": True}
    sub = {"id": "v1", "name": "Fibre Co", "code": "FC", "contact_email": "a@x",
           "is_active": True}
    assert compare_fields("vendors", crm, sub, subscriber_map={}).diffs == ()
    sub_drift = {**sub, "name": "Renamed"}
    diffs = compare_fields("vendors", crm, sub_drift, subscriber_map={}).diffs
    assert [d.field for d in diffs] == ["name"]
    assert diffs[0].crm_value == "Fibre Co" and diffs[0].sub_value == "Renamed"


def test_project_quote_decimal_drift() -> None:
    crm = {"id": "q1", "project_id": "p1", "vendor_id": "v1", "status": "approved",
           "currency": "NGN", "subtotal": Decimal("100.00"),
           "tax_total": Decimal("7.50"), "total": Decimal("107.50"), "is_active": True}
    sub = {**crm, "total": Decimal("108.00")}
    diffs = compare_fields("project_quotes", crm, sub, subscriber_map={}).diffs
    assert [d.field for d in diffs] == ["total"]


def test_installation_project_subscriber_link_resolves_and_matches() -> None:
    crm = {"id": "ip1", "project_id": "p1", "subscriber_id": "crm-sub-1",
           "assigned_vendor_id": "v1", "assignment_type": "direct",
           "status": "approved", "is_active": True}
    sub = {"id": "ip1", "project_id": "p1", "subscriber_id": "sub-1",
           "assigned_vendor_id": "v1", "assignment_type": "direct",
           "status": "approved", "is_active": True}
    comparison = compare_fields(
        "installation_projects", crm, sub, subscriber_map=SUBSCRIBER_MAP
    )
    assert comparison.diffs == ()
    assert comparison.unresolved_subscriber_reason is None


def test_installation_project_subscriber_mismatch_is_drift() -> None:
    crm = {"id": "ip1", "project_id": "p1", "subscriber_id": "crm-sub-1",
           "assigned_vendor_id": None, "assignment_type": None,
           "status": "draft", "is_active": True}
    sub = {**crm, "subscriber_id": "sub-2"}
    comparison = compare_fields(
        "installation_projects", crm, sub, subscriber_map=SUBSCRIBER_MAP
    )
    assert [d.field for d in comparison.diffs] == ["subscriber_id"]
    assert comparison.diffs[0].crm_value == "sub-1"
    assert comparison.diffs[0].sub_value == "sub-2"


def test_installation_project_subscriber_enrichment_when_crm_null() -> None:
    crm = {"id": "ip1", "project_id": "p1", "subscriber_id": None,
           "assigned_vendor_id": None, "assignment_type": None,
           "status": "draft", "is_active": True}
    sub = {**crm, "subscriber_id": "sub-9"}
    comparison = compare_fields(
        "installation_projects", crm, sub, subscriber_map=SUBSCRIBER_MAP
    )
    assert comparison.diffs == ()
    assert [e.field for e in comparison.enrichments] == ["subscriber_id"]


def test_installation_project_unmapped_subscriber_flagged() -> None:
    crm = {"id": "ip1", "project_id": "p1", "subscriber_id": "crm-sub-unknown",
           "assigned_vendor_id": None, "assignment_type": None,
           "status": "draft", "is_active": True}
    sub = {**crm, "subscriber_id": None}
    comparison = compare_fields(
        "installation_projects", crm, sub, subscriber_map=SUBSCRIBER_MAP
    )
    assert comparison.unresolved_subscriber_reason == "unmapped_crm_subscriber"


def test_route_geom_ewkt_drift_detected() -> None:
    geom = "SRID=4326;LINESTRING(3.1 6.2,3.2 6.3)"
    crm = {"id": "r1", "project_id": "p1", "status": "accepted",
           "variation_type": None, "actual_length_meters": Decimal("120.0"),
           "version": 1, "route_geom": geom, "is_active": True}
    sub_same = {**crm}
    assert compare_fields("as_built_routes", crm, sub_same, subscriber_map={}).diffs == ()
    sub_drift = {**crm, "route_geom": "SRID=4326;LINESTRING(9.9 9.9,1.0 1.0)"}
    diffs = compare_fields("as_built_routes", crm, sub_drift, subscriber_map={}).diffs
    assert [d.field for d in diffs] == ["route_geom"]


# ---------------------------------------------------------------------------
# Missing-row classification
# ---------------------------------------------------------------------------


def test_classify_missing_installation_project_native_absent() -> None:
    row = {"id": "ip1", "project_id": "proj-missing"}
    assert (
        classify_missing_row("installation_projects", row, project_ids={"proj-1"})
        == "native_project_absent"
    )


def test_classify_missing_installation_project_present_is_generic() -> None:
    row = {"id": "ip1", "project_id": "proj-1"}
    assert (
        classify_missing_row("installation_projects", row, project_ids={"proj-1"})
        == "missing"
    )


def test_classify_missing_other_table_is_generic() -> None:
    assert classify_missing_row("vendors", {"id": "v1"}, project_ids=set()) == "missing"


# ---------------------------------------------------------------------------
# Children counts
# ---------------------------------------------------------------------------


def test_children_count_and_amount_sum_mismatch() -> None:
    crm: dict[str, Any] = {"lines": 3, "lines_amount_sum": Decimal("300.00"),
                           "route_revisions": 2}
    sub: dict[str, Any] = {"lines": 3, "lines_amount_sum": Decimal("250.00"),
                           "route_revisions": 1}
    mismatches = compare_children_counts(
        crm, sub, ("lines", "lines_amount_sum", "route_revisions")
    )
    kinds = {kind for kind, _, _ in mismatches}
    assert kinds == {"lines_amount_sum", "route_revisions"}


def test_children_count_absent_treated_as_zero() -> None:
    mismatches = compare_children_counts({}, {"quotes": 2}, ("quotes",))
    assert mismatches == [("quotes", "0", "2")]
