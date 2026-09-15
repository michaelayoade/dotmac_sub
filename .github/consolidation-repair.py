"""Apply reviewed repairs after assembling the pinned original PR tree."""
from pathlib import Path

root = Path.cwd()

def replace(path, old, new):
    target = root / path
    text = target.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"Unexpected repair context in {path}: {old[:80]}")
    target.write_text(text.replace(old, new, 1))

p = "app/services/sales/service.py"
replace(p, 'class QuoteListDatePreset(StrEnum):\n    LAST_7_DAYS = "last_7_days"\n    LAST_30_DAYS = "last_30_days"\n    CUSTOM = "custom"\n\n\n', 'class QuoteListDatePreset(StrEnum):\n    LAST_7_DAYS = "last_7_days"\n    LAST_30_DAYS = "last_30_days"\n    CUSTOM = "custom"\n\n\n@dataclass(frozen=True, slots=True)\nclass QuoteListDateRange:\n    """Inclusive UTC Quote creation dates; an empty value means All time."""\n\n    preset: QuoteListDatePreset | None = None\n    date_from: date | None = None\n    date_to: date | None = None\n\n\n')
replace(p, '    lead_id: str | None = None\n    date_preset: str | None = None\n    date_from: str | None = None\n    date_to: str | None = None\n    sort_field: str | None = None\n    sort_direction: str | None = None\n    page: int = 1\n    page_size: int = 25\n', '    lead_id: str | None = None\n    sort_field: str | None = None\n    sort_direction: str | None = None\n    page: int = 1\n    page_size: int = 25\n    date_preset: str | None = None\n    date_from: str | None = None\n    date_to: str | None = None\n')
replace(p, '    lead_id: uuid.UUID | None\n    date_preset: QuoteListDatePreset | None\n    date_from: date | None\n    date_to: date | None\n    sort_field: QuoteListSortField\n    sort_direction: QuoteListSortDirection\n    page: int\n    page_size: int\n', '    lead_id: uuid.UUID | None\n    sort_field: QuoteListSortField\n    sort_direction: QuoteListSortDirection\n    page: int\n    page_size: int\n    date_preset: QuoteListDatePreset | None = None\n    date_from: date | None = None\n    date_to: date | None = None\n')
text = (root / p).read_text()
start = text.index('def normalize_lead_date_range(')
end = text.index('def _normalize_lead_list_query(', start)
quote_normalizer = text[start:end].replace('Lead', 'Quote').replace('lead', 'quote')
replace(p, 'def _optional_date_filter(value: str | None) -> date | None:\n    candidate = str(value or "").strip()\n    if not candidate:\n        return None\n    try:\n        return date.fromisoformat(candidate)\n    except ValueError:\n        return None\n\n\n', quote_normalizer)
text = (root / p).read_text()
start = text.index('    date_preset = _optional_enum_filter(request.date_preset, QuoteListDatePreset)')
end = text.index('    return QuoteListQuery(', start)
replace(p, text[start:end], '    date_range = normalize_quote_date_range(request)\n\n')
replace(p, '        date_preset=date_preset,\n        date_from=date_from,\n        date_to=date_to,\n        sort_field=(\n', '        date_preset=date_range.preset,\n        date_from=date_range.date_from,\n        date_to=date_range.date_to,\n        sort_field=(\n')
p = "app/services/sales/__init__.py"
replace(p, '    QuoteListDatePreset,\n', '    QuoteListDatePreset,\n    QuoteListDateRange,\n')
replace(p, '    normalize_quote_search,\n', '    normalize_quote_date_range,\n    normalize_quote_search,\n')
replace(p, '    "QuoteListDatePreset",\n', '    "QuoteListDatePreset",\n    "QuoteListDateRange",\n')
replace(p, '    "normalize_quote_search",\n', '    "normalize_quote_date_range",\n    "normalize_quote_search",\n')
p = "app/services/web_sales.py"
text = (root / p).read_text()
start = text.index('def _lead_date_filters(')
end = text.index('def build_leads_list_context(', start)
serializer = text[start:end].replace('Lead', 'Quote').replace('lead', 'quote')
replace(p, 'def build_quotes_list_context(\n', serializer + 'def build_quotes_list_context(\n')
replace(p, '        "date_preset": (\n            normalized.date_preset.value\n            if normalized.date_preset is not None\n            else None\n        ),\n        "date_from": normalized.date_from.isoformat() if normalized.date_from else None,\n        "date_to": normalized.date_to.isoformat() if normalized.date_to else None,\n', '        **_quote_date_filters(\n            sales_service.QuoteListDateRange(\n                preset=normalized.date_preset,\n                date_from=normalized.date_from,\n                date_to=normalized.date_to,\n            )\n        ),\n')
replace(p, '    date_preset: str | None,\n    date_from: str | None,\n    date_to: str | None,\n    search: str | None,\n', '    date_preset: str | None = None,\n    date_from: str | None = None,\n    date_to: str | None = None,\n    search: str | None,\n')
text = (root / p).read_text()
start = text.index('    normalized_date_preset = _clean_choice(', text.index('def build_quotes_failure_context'))
end = text.index('    safe_sort = (', start)
replace(p, text[start:end], '    date_range = sales_service.normalize_quote_date_range(\n        sales_service.QuoteListQueryInput(\n            date_preset=date_preset,\n            date_from=date_from,\n            date_to=date_to,\n        )\n    )\n    date_filters = _quote_date_filters(date_range)\n')
replace(p, '            "date_preset": normalized_date_preset,\n            "date_from": (\n                normalized_date_from.isoformat() if normalized_date_from else None\n            ),\n            "date_to": normalized_date_to.isoformat() if normalized_date_to else None,\n', '            **date_filters,\n')
replace(p, '        "date_preset": normalized_date_preset or "",\n        "date_from": normalized_date_from.isoformat() if normalized_date_from else "",\n        "date_to": normalized_date_to.isoformat() if normalized_date_to else "",\n', '        "date_preset": date_filters["date_preset"] or "",\n        "date_from": date_filters["date_from"] or "",\n        "date_to": date_filters["date_to"] or "",\n')

p = root / "tests/test_web_sales_quotes_list.py"
text = p.read_text().replace('from datetime import UTC, datetime, timedelta', 'from datetime import UTC, date, datetime, timedelta').replace('from fastapi import FastAPI', 'import pytest\nfrom fastapi import FastAPI', 1)
text += '''

@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-01", "9999-12-31"),
        ("9999-12-31", "9999-12-31"),
        ("2026-09-15", "2026-09-01"),
        ("not-a-date", "2026-09-15"),
        (None, "2026-09-15"),
        ("20260901", "2026-09-15"),
        ("2026-09-01", None),
    ],
)
def test_quote_date_owner_and_failure_view_reject_invalid_ranges(start, end):
    request = sales.QuoteListQueryInput(
        date_preset="custom", date_from=start, date_to=end
    )
    assert sales.normalize_quote_date_range(request) == sales.QuoteListDateRange()
    context = web_sales.build_quotes_failure_context(
        status=None, lead_id=None, search=None, sort_by=None, sort_dir=None,
        page=1, per_page=25, date_preset="custom", date_from=start, date_to=end,
    )
    assert context["date_preset"] == ""
    assert context["date_from"] == context["date_to"] == ""
    assert "date_preset=" not in context["retry_url"]


@pytest.mark.parametrize(
    ("preset", "today", "start"),
    [
        ("last_7_days", date(2026, 1, 3), date(2025, 12, 28)),
        ("last_30_days", date(2024, 3, 1), date(2024, 2, 1)),
        ("last_30_days", date.min, date.min),
    ],
)
def test_quote_date_owner_resolves_calendar_boundaries(preset, today, start):
    result = sales.normalize_quote_date_range(
        sales.QuoteListQueryInput(date_preset=preset), today=today
    )
    assert result.date_from == start
    assert result.date_to == today
    assert result.preset == sales.QuoteListDatePreset(preset)


def test_quote_maximum_end_date_does_not_overflow_query(db_session):
    result = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(
            date_preset="custom", date_from="2026-01-01", date_to="9999-12-31"
        ),
    )
    assert result.query.date_preset is None
    assert result.query.date_from is result.query.date_to is None


def test_quote_date_fields_preserve_legacy_positional_constructors():
    request = sales.QuoteListQueryInput(
        "needle", "sent", None, "updated_at", "asc", 2, 10
    )
    assert request.sort_field == "updated_at"
    assert request.sort_direction == "asc"
    assert (request.page, request.page_size) == (2, 10)
    assert request.date_preset is request.date_from is request.date_to is None
    query = sales.QuoteListQuery(
        None, None, None,
        sales.QuoteListSortField.UPDATED_AT,
        sales.QuoteListSortDirection.ASC,
        2, 10,
    )
    assert query.offset == 10
    assert query.date_preset is query.date_from is query.date_to is None


def test_quote_relative_bookmarks_and_retry_do_not_freeze_dates(db_session):
    params = {
        "status": None, "lead_id": None, "search": None,
        "sort_by": None, "sort_dir": None, "page": 1, "per_page": 25,
        "date_preset": "last_7_days",
        "date_from": "2000-01-01", "date_to": "2000-01-02",
    }
    context = web_sales.build_quotes_list_context(db_session, **params)
    retry = web_sales.build_quotes_failure_context(**params)
    for state in (context, retry):
        assert state["list_query"].filter_value("date_preset") == "last_7_days"
        assert state["list_query"].filter_value("date_from") is None
        assert state["list_query"].filter_value("date_to") is None
    assert context["canonicalization_needed"] is True


def test_quote_unavailable_view_retains_valid_custom_range():
    context = web_sales.build_quotes_failure_context(
        status="sent", lead_id=None, search=None,
        sort_by=None, sort_dir=None, page=1, per_page=25,
        date_preset="custom", date_from="2026-09-01", date_to="2026-09-15",
    )
    assert context["date_from"] == "2026-09-01"
    assert context["date_to"] == "2026-09-15"
    assert "date_from=2026-09-01" in context["retry_url"]
    assert "date_to=2026-09-15" in context["retry_url"]
'''
p.write_text(text)
p = root / "tests/architecture/test_sales_quote_list_query_boundary.py"
p.write_text(p.read_text() + '''

def test_quote_success_and_failure_share_the_public_date_owner() -> None:
    owner = inspect.getsource(sales_service._normalize_quote_list_query)
    recovery = inspect.getsource(web_sales.build_quotes_failure_context)
    assert "normalize_quote_date_range(request)" in owner
    assert "sales_service.normalize_quote_date_range(" in recovery
    assert "sales_service.QuoteListQueryInput(" in recovery
    for source in (recovery, inspect.getsource(web_sales.build_quotes_list_context)):
        assert "_optional_date_filter" not in source
        assert "fromisoformat" not in source
        assert "timedelta" not in source
''')
p = root / "tests/integration/test_quote_list_search_postgres.py"
p.write_text(p.read_text() + '''

def test_quote_maximum_custom_end_canonicalizes_without_overflow(
    db_session, quote_search_graph
):
    all_time = sales.quotes.query(db_session, sales.QuoteListQueryInput())
    invalid = sales.quotes.query(
        db_session,
        sales.QuoteListQueryInput(
            date_preset="custom", date_from="2026-01-01", date_to="9999-12-31"
        ),
    )
    assert invalid.query.date_preset is None
    assert invalid.query.date_from is invalid.query.date_to is None
    assert invalid.total_count == all_time.total_count
    assert [row.id for row in invalid.items] == [row.id for row in all_time.items]
''')
replace("docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md", '  changing the form resets page to one and Reset clears the complete scope.\n', '  changing the form resets page to one and Reset clears the complete scope.\n  `normalize_quote_date_range` is the public date-policy owner used by both\n  successful reads and unavailable retry views. Custom dates must be canonical\n  ISO dates; an end date of 9999-12-31 becomes All time before constructing its\n  unrepresentable exclusive next-day bound. Relative bookmarks carry only the\n  preset. Appended optional fields preserve legacy typed query constructors.\n')
replace("docs/SOT_RELATIONSHIP_MAP.md", '## What counts as an adapter\n', '## Quote creation-date query ownership\n\n`sales.service` owns `QuoteListDateRange` and `normalize_quote_date_range`.\nThe normal query and database-unavailable retry adapter use this same public\nowner; adapters do not import private date parsers or interpret date ranges.\nInvalid and unrepresentable dates canonicalize to All time before timestamp\nbounds are constructed. Relative bookmarks carry only their preset.\nEnforcement: `tests/architecture/test_sales_quote_list_query_boundary.py`,\n`tests/test_web_sales_quotes_list.py`, and the migrated PostgreSQL quote-list\nacceptance tests. No new writer, owner, schema, or permission is introduced.\n\n## What counts as an adapter\n')
replace("app/services/sot_registry/domains/sales_referrals/lifecycle.py", '            "the same inclusive UTC scope supplies rows, count, summary, and retry."\n', '            "the same inclusive UTC scope supplies rows, count, summary, and retry. "\n            "Quote dates likewise use public normalize_quote_date_range for query "\n            "and retry; invalid or unrepresentable bounds become All time."\n')
print("Applied reviewed Quote date-owner, retry, boundary, compatibility, and regression repairs")
