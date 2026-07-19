"""Catalog overview headline tiles are Kpi contracts that drill into their cohort.

The catalog dashboard tiles (active offers, total plans, active subscriptions,
archived) used to be raw counts the template rendered with no drill-down. They
now come back as ``Kpi`` objects whose ``cohort_url`` opens exactly the filtered
list that produced the number (KPI-parity), so a tile and its list can never
disagree. The active-subscriptions tile crosses into the subscriptions list.
"""

from __future__ import annotations

from pathlib import Path

from app.models.catalog import OfferStatus, SubscriptionStatus
from app.schemas.status_presentation import StatusTone
from app.services import web_catalog_offers
from app.services.ui_contracts import Kpi, StateValue

_SAMPLE_STATS: dict[str, object] = {
    "total_count": 12,
    "active_count": 7,
    "archived_count": 3,
    "total_subscriptions": 25,
}


def test_catalog_overview_kpis_are_contracts_carrying_present_state():
    kpis = web_catalog_offers.catalog_overview_kpis(_SAMPLE_STATS)
    assert set(kpis) == {
        "active_offers",
        "total_plans",
        "active_subscriptions",
        "archived",
    }
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())
    assert all(isinstance(kpi.value, StateValue) for kpi in kpis.values())
    # Counts are always resolved, so they are present state, never a zero
    # standing in for the unknown.
    assert all(kpi.value.is_present for kpi in kpis.values())
    assert kpis["active_offers"].value.value == 7
    assert kpis["total_plans"].value.value == 12
    assert kpis["active_subscriptions"].value.value == 25
    assert kpis["archived"].value.value == 3


def test_catalog_overview_kpi_cohort_urls_are_relative_and_filtered():
    kpis = web_catalog_offers.catalog_overview_kpis(_SAMPLE_STATS)
    for kpi in kpis.values():
        assert kpi.cohort_url.startswith("/")

    # Offer tiles narrow the offer list by status; total plans keeps the whole
    # list; active subscriptions crosses into the subscriptions list filtered to
    # the exact status its count query used.
    assert kpis["active_offers"].cohort_url == (
        f"/admin/catalog?status={OfferStatus.active.value}"
    )
    assert kpis["total_plans"].cohort_url == "/admin/catalog"
    assert kpis["archived"].cohort_url == (
        f"/admin/catalog?status={OfferStatus.archived.value}"
    )
    assert kpis["active_subscriptions"].cohort_url == (
        f"/admin/catalog/subscriptions?status={SubscriptionStatus.active.value}"
    )


def test_catalog_overview_kpi_tones_are_semantic():
    kpis = web_catalog_offers.catalog_overview_kpis(_SAMPLE_STATS)
    assert kpis["active_offers"].tone is StatusTone.positive
    assert kpis["active_subscriptions"].tone is StatusTone.info
    assert kpis["total_plans"].tone is StatusTone.neutral
    assert kpis["archived"].tone is StatusTone.neutral


def test_catalog_overview_kpis_default_missing_counts_to_zero_present():
    # A partial stats payload must still yield present zero tiles, never crash.
    kpis = web_catalog_offers.catalog_overview_kpis({})
    assert all(kpi.value.is_present for kpi in kpis.values())
    assert kpis["total_plans"].value.value == 0


def test_catalog_index_template_renders_kpi_cohort_links():
    source = (
        Path(__file__).resolve().parents[1] / "templates/admin/catalog/index.html"
    ).read_text(encoding="utf-8")
    # Tiles deep-link and render the StateValue, not a bare count.
    for key in ("active_offers", "total_plans", "active_subscriptions", "archived"):
        assert f"catalog_stats.kpis.{key}.value.value" in source
        assert f"href=catalog_stats.kpis.{key}.cohort_url" in source
        assert f"tone=catalog_stats.kpis.{key}.tone" in source
    # The old bare-count references are gone.
    assert "catalog_stats.active_count" not in source
    assert "catalog_stats.archived_count" not in source
