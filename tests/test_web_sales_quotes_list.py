"""Admin quotes list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from app.services import web_sales


def test_quote_list_definition_declares_its_capabilities():
    definition = web_sales.QUOTE_LIST_DEFINITION
    assert set(definition.sortable_keys) == {"created_at", "updated_at"}
    assert set(definition.filterable_keys) == {"status", "lead_id"}
    assert definition.default_sort == "created_at"


def test_build_quotes_list_context_exposes_list_query_and_page_meta(db_session):
    ctx = web_sales.build_quotes_list_context(
        db_session, status=None, lead_id=None, search=None, page=1, per_page=25
    )
    assert "list_query" in ctx
    assert "page_meta" in ctx
    assert ctx["page"] == ctx["page_meta"].page
    assert ctx["total"] == ctx["page_meta"].total_items


def test_build_quotes_list_context_normalizes_stale_params(db_session):
    ctx = web_sales.build_quotes_list_context(
        db_session,
        status="not-a-status",
        lead_id=None,
        search=None,
        sort_by="status",  # filterable, not sortable
        sort_dir="sideways",
        page=1,
        per_page=999,
    )
    query = ctx["list_query"]
    assert query.sort_by == "created_at"
    assert query.sort_dir == "desc"
    assert query.per_page == 25
    assert query.filter_value("status") is None
