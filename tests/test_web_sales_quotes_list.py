"""Admin quotes list is routed through list_query (Carbon/WCAG list standard)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.models.sales import Quote
from app.services import sales, web_sales


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
        lead_id="not-a-uuid",
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
    assert query.filter_value("lead_id") is None
    assert ctx["canonicalization_needed"] is True


def test_quote_list_has_a_stable_id_tie_breaker_across_pages(db_session, subscriber):
    created = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            Quote(id=UUID(int=value), subscriber_id=subscriber.id, created_at=created)
            for value in (4, 1, 3, 2)
        ]
    )
    db_session.commit()

    first = sales.quotes.list(db_session, None, None, None, "created_at", "desc", 2, 0)
    second = sales.quotes.list(db_session, None, None, None, "created_at", "desc", 2, 2)

    assert [row.id.int for row in first + second] == [1, 2, 3, 4]


def test_quote_id_search_count_matches_returned_rows(db_session, subscriber):
    quote = Quote(subscriber_id=subscriber.id)
    db_session.add(quote)
    db_session.commit()
    search = str(quote.id)

    ctx = web_sales.build_quotes_list_context(
        db_session,
        status=None,
        lead_id=None,
        search=search,
        page=1,
        per_page=25,
    )

    assert ctx["total"] == 1
    assert [row.id for row in ctx["quotes"]] == [quote.id]
