"""Persistent invoice controls preserve canonical list state and permissions."""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.list_query import ListDefinition, ListFieldDefinition


class Options(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "option" and (value := attributes.get("value")):
            self.values[value] = attributes


def test_invoice_sort_preserves_filters_and_resets_pagination() -> None:
    definition = ListDefinition(
        key="billing_invoices",
        fields=tuple(
            ListFieldDefinition(key, key, sortable=True)
            for key in (
                "created_at",
                "issued_at",
                "due_at",
                "invoice_number",
                "total",
                "status",
            )
        )
        + tuple(
            ListFieldDefinition(key, key, filterable=True)
            for key in ("customer_ref", "start_date", "end_date")
        ),
        default_sort="created_at",
    )
    query = definition.build_query(
        search="invoice reference",
        filters={
            "customer_ref": "person:123",
            "start_date": "2026-11-01",
            "end_date": "2026-11-09",
        },
        page=3,
        per_page=50,
    )
    environment = Environment(
        loader=FileSystemLoader("templates"), autoescape=select_autoescape(default=True)
    )
    environment.filters["app_datetime"] = lambda value, *_args, **_kwargs: value
    template = environment.get_template("admin/billing/_invoices_table.html")
    context: dict[str, object] = {
        "list_query": query,
        "invoices": [],
        "request": None,
        "page_meta": SimpleNamespace(
            start_item=0, end_item=0, total_items=0, total_pages=1
        ),
        "invoice_bulk_action_contract": {"selection_enabled": True},
    }
    html = template.render(**context)
    options = Options()
    options.feed(html)
    for value in (
        "created_at:asc",
        "created_at:desc",
        "issued_at:asc",
        "issued_at:desc",
    ):
        url = options.values[value]["data-url"]
        assert url is not None
        params = parse_qs(urlsplit(url).query)
        sort, direction = value.split(":")
        assert params["sort"] == [sort]
        assert params["dir"] == [direction]
        assert params["search"] == ["invoice reference"]
        assert params["customer_ref"] == ["person:123"]
        assert params["start_date"] == ["2026-11-01"]
        assert params["end_date"] == ["2026-11-09"]
        assert params["per_page"] == ["50"]
        assert params["page"] == ["1"]
    assert "selected" in options.values["created_at:desc"]
    assert html.index('id="invoice-select-page"') < html.index("<table")
    assert html.index('aria-label="Sort invoices"') < html.index("<table")
    context["invoice_bulk_action_contract"] = {"selection_enabled": False}
    assert 'id="invoice-select-page"' not in template.render(**context)


def test_changed_stylesheets_use_fresh_cache_keys() -> None:
    base = Path("templates/base.html").read_text(encoding="utf-8")
    assert "/static/css/main.css?v=20261109-billing2" in base
    assert "/static/css/design-system.css?v=20261109-billing2" in base
    assert "/static/css/main.css?v=20260829a" not in base
    assert "/static/css/design-system.css?v=20260914a" not in base
