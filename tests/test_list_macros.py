"""Render tests for the reusable list sort_header macro."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


class _StubQuery:
    """Minimal stand-in for list_query.ListQuery used by sort_header."""

    def __init__(self, sort_by: str, sort_dir: str) -> None:
        self.sort_by = sort_by
        self.sort_dir = sort_dir

    def url(self, base: str, **params) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base}?{query}"


def _render(sort_by: str, sort_dir: str, key: str) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    tmpl = env.from_string(
        "{% from 'components/ui/list_macros.html' import sort_header %}"
        "{{ sort_header(lq, '/admin/referrals', key, 'Created', entity='referrals') }}"
    )
    return tmpl.render(lq=_StubQuery(sort_by, sort_dir), key=key)


def test_active_descending_column_toggles_to_ascending_and_marks_aria_sort():
    html = _render("created_at", "desc", "created_at")
    assert 'aria-sort="descending"' in html
    # Clicking an active-descending column sorts ascending next, resetting page.
    # (& is HTML-escaped to &amp; in the href, which is correct.)
    assert "/admin/referrals?page=1&amp;sort_by=created_at&amp;sort_dir=asc" in html
    assert "Sort referrals by created ascending" in html


def test_inactive_column_is_aria_sort_none_and_links_ascending():
    html = _render("created_at", "desc", "status")
    assert 'aria-sort="none"' in html
    assert "/admin/referrals?page=1&amp;sort_by=status&amp;sort_dir=asc" in html
