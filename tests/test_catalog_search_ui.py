"""Catalog search keeps its input outside the HTMX result swap."""

from __future__ import annotations

from html.parser import HTMLParser

from jinja2 import Environment, FileSystemLoader


class _CatalogGridParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside_results = False
        self.depth = 0
        self.search_outside_results = False
        self.result_controls: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if attributes.get("id") == "catalog-grid-results":
            self.inside_results = True
            self.depth = 1
        elif self.inside_results:
            self.depth += 1
        if attributes.get("name") == "search":
            self.search_outside_results = not self.inside_results
            self.result_controls.append(attributes)
        elif self.inside_results and "hx-get" in attributes:
            self.result_controls.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if self.inside_results:
            self.depth -= 1
            if self.depth == 0:
                self.inside_results = False


def test_catalog_search_swaps_results_without_replacing_the_input() -> None:
    template = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    html = template.get_template("admin/catalog/_grid.html").render(
        offers=[],
        offer_statuses=[],
        plan_families=[],
        total=50,
        page=2,
        total_pages=3,
        per_page=25,
    )
    parser = _CatalogGridParser()
    parser.feed(html)

    assert parser.search_outside_results
    assert len(parser.result_controls) >= 4
    assert all(
        control["hx-target"] == "#catalog-grid-results"
        and control["hx-select"] == "#catalog-grid-results"
        for control in parser.result_controls
    )
    assert 'hx-sync="#catalog-grid-wrapper:replace"' in html
