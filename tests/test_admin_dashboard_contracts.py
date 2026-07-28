"""Admin dashboard headline tiles are Kpi/StateValue contracts.

The overview tiles used to be raw values the template formatted itself, with a
decimal 0 rendered whether a source held zero or had failed. They now come from
``build_dashboard_kpis`` as ``Kpi`` objects whose ``cohort_url`` drills into the
exact admin list the number counts (KPI-parity), and whose money/online values
carry their own ``StateValue`` state so an unresolved source renders "Unknown",
never a lying 0.
"""

from __future__ import annotations

from pathlib import Path

from app.services.ui_contracts import Kpi, StateValue
from app.services.web_admin_dashboard import build_dashboard_kpis


def _kpis(*, billing_ok=True, online_value=None):
    return build_dashboard_kpis(
        total_subscribers=1200,
        online_sessions_value=online_value or StateValue.present(340),
        devices_working=48,
        devices_total=50,
        payments_this_month=1_500_000.0,
        overdue_amount=250_000.0,
        total_alarms=3,
        billing_ok=billing_ok,
    )


def test_headline_tiles_are_kpis_that_drill_into_their_cohort():
    kpis = _kpis()
    assert set(kpis) == {
        "total_subscribers",
        "online",
        "network_devices",
        "collections",
        "overdue",
        "alarms",
    }
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())

    # Every cohort URL is application-relative and points at the matching list.
    for kpi in kpis.values():
        assert kpi.cohort_url.startswith("/")
    assert kpis["total_subscribers"].cohort_url == "/admin/customers"
    assert kpis["online"].cohort_url == "/admin/network/sessions"
    assert kpis["online"].label == "Online Sessions"
    assert kpis["network_devices"].cohort_url == "/admin/network/monitoring"
    assert kpis["alarms"].cohort_url == "/admin/network/alarms"

    # Collections is the billing-overview collected figure (invoice settled
    # value over the calendar month). No itemized list sums to exactly that
    # aggregate, so it drills into /admin/billing, which displays the identical
    # number — parity by same-number, not by a mismatched payments list.
    assert kpis["collections"].cohort_url == "/admin/billing"

    # Overdue drills into the invoices list filtered to exactly the overdue cohort.
    assert kpis["overdue"].cohort_url == "/admin/billing/invoices?status=overdue"


def test_present_tiles_carry_their_computed_value():
    kpis = _kpis()
    assert kpis["total_subscribers"].value == StateValue.present(1200)
    assert kpis["network_devices"].value.value == "48 / 50"
    assert kpis["collections"].value.value == "₦1,500,000"
    assert kpis["overdue"].value.value == "₦250,000"
    assert kpis["alarms"].value.value == 3


def test_money_tiles_go_unknown_when_billing_read_owner_is_down():
    # A failed billing read must not render as ₦0 — it renders Unknown so the
    # figure and the AR reality can never silently disagree.
    kpis = _kpis(billing_ok=False)
    assert not kpis["collections"].value.is_present
    assert not kpis["overdue"].value.is_present
    assert kpis["collections"].value.placeholder == "Unknown"
    assert kpis["overdue"].value.placeholder == "Unknown"


def test_online_tile_reflects_an_unresolved_radius_read():
    # A failed RADIUS read renders Unknown, never a 0 that reads as "nobody online".
    kpis = _kpis(online_value=StateValue.unknown())
    assert not kpis["online"].value.is_present
    assert kpis["online"].value.placeholder == "Unknown"


def test_stats_template_renders_the_kpi_contract_fields():
    source = (
        Path(__file__).resolve().parents[1] / "templates/admin/dashboard/_stats.html"
    ).read_text(encoding="utf-8")
    # Tiles read the contract value and its drill-down, not bare stats values.
    assert "dashboard_kpis.total_subscribers.value.value" in source
    assert "dashboard_kpis.total_subscribers.cohort_url" in source
    assert "dashboard_kpis.overdue.cohort_url" in source
    assert "dashboard_kpis.alarms.value.value" in source
    # State-bearing tiles guard on is_present before formatting the value.
    assert "online_v.is_present" in source
    assert "collections_v.is_present" in source
    assert "overdue_v.is_present" in source


def test_index_template_renders_freshness_as_state():
    source = (
        Path(__file__).resolve().parents[1] / "templates/admin/dashboard/index.html"
    ).read_text(encoding="utf-8")
    # The snapshot freshness is a StateValue, rendered only when present.
    assert "data_freshness.is_present" in source
    assert "data_freshness.value" in source
