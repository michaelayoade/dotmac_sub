"""Keep the admin money cards on their declared read owners."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_does_not_restore_broader_revenue_or_past_due_cohorts() -> None:
    source = (ROOT / "app/services/web_admin_dashboard.py").read_text(encoding="utf-8")
    assert "BillingReporting.get_admin_dashboard_collections(" in source
    assert "dashboard_overdue_status_balance(db)" in source
    assert "BillingReporting.get_overview_stats(" not in source
    assert "overdue_debt_filters(" not in source
