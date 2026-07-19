"""Reseller accounts list is routed through list_query (Carbon/WCAG standard)."""

from __future__ import annotations

from pathlib import Path

from app.services import web_reseller_routes


def test_reseller_account_list_definition_declares_its_capabilities():
    definition = web_reseller_routes.RESELLER_ACCOUNT_LIST_DEFINITION
    # Sortable keys mirror the reseller_portal.list_accounts order_by whitelist.
    assert set(definition.sortable_keys) == {"name", "balance", "overdue", "created_at"}
    assert set(definition.filterable_keys) == {"status_filter"}
    assert definition.default_sort == "created_at"
    # The reseller portal's page size is 20, which the definition must allow.
    assert definition.default_per_page == 20
    assert 20 in definition.per_page_options


def test_reseller_account_definition_round_trips_state_in_the_url():
    definition = web_reseller_routes.RESELLER_ACCOUNT_LIST_DEFINITION
    # A valid sort + filter builds a URL that round-trips the state.
    query = definition.build_query(
        search=None,
        filters={"status_filter": "suspended"},
        sort_by="balance",
        sort_dir="asc",
        page=1,
        per_page=20,
    )
    assert query.sort_by == "balance"
    assert query.filter_value("status_filter") == "suspended"
    url = query.url("/reseller/accounts", page=2)
    assert "sort=balance" in url
    assert "dir=asc" in url
    assert "status_filter=suspended" in url
    assert "page=2" in url


def test_reseller_accounts_template_uses_shared_list_controls():
    source = (
        Path(__file__).resolve().parents[1] / "templates/reseller/accounts/index.html"
    ).read_text(encoding="utf-8")
    assert 'name="sort" value="{{ list_query.sort_by }}"' in source
    assert 'name="dir" value="{{ list_query.sort_dir }}"' in source
    assert "list_pagination(list_query, page_meta" in source
