"""Ownership and bounded-query guards for the Upcoming Charges report."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/billing/reporting.py"
PRESENTER = ROOT / "app/services/web_reports_extended.py"
ROUTE = ROOT / "app/web/admin/reports.py"
MIGRATION = ROOT / "alembic/versions/559_upcoming_charges_indexes.py"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} was not found in {path}")


def test_web_presenter_delegates_the_query_to_registered_owner() -> None:
    source = _function_source(PRESENTER, "get_upcoming_charges_data")

    assert "UpcomingChargesQuery(" in source
    assert "get_upcoming_charges_page(" in source
    assert "select(" not in source
    assert ".query(" not in source


def test_owner_bounds_candidates_before_prepaid_enrichment() -> None:
    owner = OWNER.read_text(encoding="utf-8")
    prepaid = _function_source(OWNER, "_prepaid_upcoming_charges")

    assert "per_page = max(10, min(query.per_page, 50))" in owner
    assert ".limit(per_page + 1)" in prepaid
    assert "records = records[:per_page]" in prepaid
    assert "filters.append(or_(*band_filters))" in prepaid
    assert prepaid.index("records = records[:per_page]") < prepaid.index(
        "resolve_prepaid_monthly_charges("
    )
    assert "prepaid_available_balances(" in prepaid


def test_route_keeps_billing_modes_lazy_and_preserves_report_name() -> None:
    route = _function_source(ROUTE, "reports_upcoming_charges")

    assert '"Upcoming Charges"' in route
    assert "mode=mode" in route
    assert "get_upcoming_charges_data(" in route


def test_migration_adds_both_candidate_window_indexes() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "ix_invoices_upcoming_collectible_due" in migration
    assert "ix_service_entitlements_active_end_subscription" in migration
    assert "ix_service_entitlements_active_subscription_end" in migration
    assert "CREATE INDEX{concurrently}" in migration
