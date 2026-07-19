"""The coarse reports:billing / reports:network permissions are split into
:read / :export, so viewing a report no longer implies the right to export its
data. The export controls are gated on the :export permission in the UI.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = (ROOT / "app/web/admin/reports.py").read_text(encoding="utf-8")
SEED = (ROOT / "scripts/seed/seed_rbac.py").read_text(encoding="utf-8")


def test_reports_routes_use_granular_permissions_not_the_coarse_keys():
    assert 'require_permission("reports:billing")' not in REPORTS
    assert 'require_permission("reports:network")' not in REPORTS
    assert 'require_permission("reports:billing:read")' in REPORTS
    assert 'require_permission("reports:billing:export")' in REPORTS
    assert 'require_permission("reports:network:read")' in REPORTS
    assert 'require_permission("reports:network:export")' in REPORTS


def test_seed_catalog_defines_granular_reports_permissions():
    for key in (
        "reports:billing:read",
        "reports:billing:export",
        "reports:network:read",
        "reports:network:export",
    ):
        assert f'("{key}"' in SEED, key
    # The coarse catalog entries are retired.
    assert '("reports:billing",' not in SEED
    assert '("reports:network",' not in SEED


def test_export_controls_are_gated_on_the_export_permission():
    revenue = (ROOT / "templates/admin/reports/revenue.html").read_text(
        encoding="utf-8"
    )
    network = (ROOT / "templates/admin/reports/network.html").read_text(
        encoding="utf-8"
    )
    bandwidth = (ROOT / "templates/admin/reports/bandwidth.html").read_text(
        encoding="utf-8"
    )
    assert 'can(request, "reports:billing:export")' in revenue
    assert 'can(request, "reports:network:export")' in network
    assert 'can(request, "reports:network:export")' in bandwidth
