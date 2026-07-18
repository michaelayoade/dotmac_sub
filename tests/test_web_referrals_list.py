"""Referrals list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from app.services import web_referrals


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


def test_list_data_accepts_valid_sort_and_filter(db_session):
    data = web_referrals.list_data(
        db_session, status="pending", sort_by="status", sort_dir="asc"
    )
    query = data["list_query"]
    assert query.sort_by == "status"
    assert query.sort_dir == "asc"
    assert query.filter_value("status") == "pending"
