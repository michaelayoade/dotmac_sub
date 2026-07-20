"""Referrals list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.services import web_referrals
from app.services.ui_contracts import Kpi


def test_reward_display_uses_program_currency_when_reward_has_no_snapshot():
    referral = SimpleNamespace(reward_amount=Decimal("25.00"), reward_currency=None)

    assert (
        web_referrals._reward_display(referral, program_currency="USD") == "USD 25.00"
    )


def test_referral_list_definition_declares_its_capabilities():
    definition = web_referrals.REFERRAL_LIST_DEFINITION
    assert set(definition.sortable_keys) == {"created_at", "status"}
    assert set(definition.filterable_keys) == {"status", "reward_status"}
    assert definition.default_sort == "created_at"
    assert definition.default_sort_dir == "desc"


def test_list_data_exposes_list_query_and_page_meta(db_session):
    data = web_referrals.list_data(db_session)
    assert "list_query" in data
    assert "page_meta" in data
    # Legacy keys stay for template compatibility during the transition.
    assert data["page"] == data["page_meta"].page
    assert data["total"] == data["page_meta"].total_items
    assert data["total_pages"] == data["page_meta"].total_pages
    assert data["list_query"].page == data["page_meta"].page
    assert data["canonicalization_needed"] is False


def test_list_data_normalizes_stale_bookmark_params(db_session):
    # A stale bookmark (unsortable field, bad direction, unsupported page size)
    # degrades to the default view instead of raising.
    data = web_referrals.list_data(
        db_session,
        status="not-a-status",
        sort_by="reward_status",  # filterable but not sortable
        sort_dir="sideways",
        per_page=999,
    )
    query = data["list_query"]
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.per_page == 25
    # An unknown status filter value is cleared, not applied.
    assert query.filter_value("status") is None
    assert data["canonicalization_needed"] is True


def test_list_data_accepts_valid_sort_and_filter(db_session):
    data = web_referrals.list_data(
        db_session, status="pending", sort_by="status", sort_dir="asc"
    )
    query = data["list_query"]
    assert query.sort_by == "status"
    assert query.sort_dir == "asc"
    assert query.filter_value("status") == "pending"


def test_list_data_clamps_page_into_the_returned_canonical_query(db_session):
    data = web_referrals.list_data(db_session, page=999)

    assert data["page_meta"].page == 1
    assert data["list_query"].page == 1
    assert data["canonicalization_needed"] is True
    assert "page=1" in data["list_query"].url("/admin/referrals")


def test_referral_kpis_link_to_the_exact_filterable_cohorts(db_session):
    stats = web_referrals.list_data(db_session)["stats"]

    assert all(
        isinstance(stats[key], Kpi)
        for key in ("total", "pending", "qualified", "rewarded")
    )
    assert stats["total"].cohort_url == (
        "/admin/referrals?sort=created_at&dir=desc&page=1&per_page=25"
    )
    assert "status=pending" in stats["pending"].cohort_url
    assert "status=qualified" in stats["qualified"].cohort_url
    assert "status=rewarded" in stats["rewarded"].cohort_url


def test_referral_template_consumes_owner_links_and_shared_list_controls():
    source = (
        Path(__file__).resolve().parents[1] / "templates/admin/referrals/index.html"
    ).read_text(encoding="utf-8")

    assert "href=stats.total.cohort_url" in source
    assert "href=stats.pending.cohort_url" in source
    assert 'name="sort" value="{{ list_query.sort_by }}"' in source
    assert 'name="dir" value="{{ list_query.sort_dir }}"' in source
    assert 'name="per_page" value="{{ list_query.per_page }}"' in source
    assert "{% call row_actions() %}" in source
    assert "list_pagination(list_query, page_meta" in source
    assert "group-hover:opacity-100" not in source
