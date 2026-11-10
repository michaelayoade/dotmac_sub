"""Billing destinations and return URLs stay valid without executing money commands."""

from __future__ import annotations

import ast
from collections.abc import Callable
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Route

ROOT = Path(__file__).resolve().parents[1]
INVOICE_ID = UUID("4a540df2-847a-4cf4-aea9-d6b3cd81a9ca")


async def _endpoint(_request: Request) -> Response:
    raise AssertionError("Route matching must not execute financial actions")


def _dunning_post_routes() -> list[Route]:
    tree = ast.parse(
        (ROOT / "app/web/admin/billing_dunning.py").read_text(encoding="utf-8")
    )
    routes = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in reversed(node.decorator_list):
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and ast.unparse(decorator.func) == "router.post"
            ):
                path = ast.literal_eval(decorator.args[0])
                routes.append(
                    Route(
                        "/billing" + path, _endpoint, methods=["POST"], name=node.name
                    )
                )
    return routes


@pytest.mark.parametrize("action", ["pause", "resume", "close"])
def test_bulk_dunning_confirmation_is_not_shadowed(action: str) -> None:
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/billing/dunning/bulk/{action}/confirm",
        "root_path": "",
    }
    first = next(
        route
        for route in _dunning_post_routes()
        if route.matches(scope)[0] is Match.FULL
    )
    assert first.name == "confirm_bulk_action"
    assert first.matches(scope)[1]["path_params"] == {"action": action}
    # Negative control: restoring the original generic converter must expose the bug.
    old_routes = [
        Route(
            route.path.replace("{case_id:uuid}", "{case_id}"),
            _endpoint,
            methods=["POST"],
            name=route.name,
        )
        for route in _dunning_post_routes()
    ]
    old_first = next(
        route for route in old_routes if route.matches(scope)[0] is Match.FULL
    )
    assert old_first.name == "confirm_case_action"


def test_single_dunning_confirmation_still_accepts_uuid_case_ids() -> None:
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/billing/dunning/{INVOICE_ID}/pause/confirm",
        "root_path": "",
    }
    first = next(
        route
        for route in _dunning_post_routes()
        if route.matches(scope)[0] is Match.FULL
    )
    assert first.name == "confirm_case_action"
    assert first.matches(scope)[1]["path_params"]["case_id"] == INVOICE_ID
    scope["method"] = "GET"
    assert not any(
        route.matches(scope)[0] is Match.FULL for route in _dunning_post_routes()
    )


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (href := dict(attrs).get("href")):
            self.hrefs.append(href)


def test_consolidated_members_use_permission_scoped_customer_details() -> None:
    source = (ROOT / "templates/admin/billing/consolidated/detail.html").read_text(
        encoding="utf-8"
    )
    loop = source.index("{% for s in subscribers %}")
    source = source[
        source.rfind("<table", 0, loop) : source.index("</table>", loop) + 8
    ]
    env = Environment(autoescape=select_autoescape(default=True))
    env.globals["can"] = lambda _request, scope: scope == "customer:read"
    template = env.from_string(source)
    subscribers = [
        SimpleNamespace(
            subscriber_id=INVOICE_ID,
            subscriber_name="Example & Sons",
            open_invoice_count=2,
            open_balance=12.34,
        )
    ]
    context = {
        "subscribers": subscribers,
        "billing_account": SimpleNamespace(currency="NGN"),
        "request": None,
    }
    parser = Links()
    parser.feed(template.render(**context))
    assert parser.hrefs == [f"/admin/customers/person/{INVOICE_ID}"]
    env.globals["can"] = lambda _request, _scope: False
    denied = template.render(**context)
    assert "href=" not in denied
    assert "Example &amp; Sons" in denied


def test_billing_overview_does_not_offer_a_placeholder_export() -> None:
    source = (ROOT / "templates/admin/billing/index.html").read_text(encoding="utf-8")
    imports = source[
        source.index("{% from") : source.index("%}", source.index("{% from")) + 2
    ]
    start = source.index('<div class="flex flex-col gap-3 lg:flex-row')
    header = source[start : source.index("{% set period_options", start)]
    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(default=True),
    )
    rendered = env.from_string(imports + header).render()
    parser = Links()
    parser.feed(rendered)
    assert parser.hrefs == ["/admin/billing/invoices", "/admin/billing/payments"]
    assert "Export" not in rendered


@cache
def _return_url() -> Callable[..., str]:
    path = ROOT / "app/web/admin/billing_invoice_actions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_invoice_return_url"
    )
    scope = {
        "UUID": UUID,
        "Literal": Literal,
        "urlsplit": urlsplit,
        "urlunsplit": urlunsplit,
        "parse_qsl": parse_qsl,
        "urlencode": urlencode,
    }
    exec(  # noqa: S102 -- load the real pure helper without importing the app stack
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), scope
    )
    return cast(Callable[..., str], scope["_invoice_return_url"])


@pytest.mark.parametrize("detail_default", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://example.test/missing",
        "//example.test/missing",
        "/\\example.test/missing",
        "javascript:alert(1)",
        "/admin/missing",
        "/admin/billing/invoices/missing",
        "/admin/billing/invoices/00000000-0000-0000-0000-000000000001",
        "/admin/billing/invoices/../payments",
        "/admin/billing/%69nvoices",
        "/admin/billing/invoices\n",
        "http://[invalid",
    ],
)
def test_invoice_return_rejects_external_or_unknown_destinations(
    value: str | None, detail_default: bool
) -> None:
    expected = (
        f"/admin/billing/invoices/{INVOICE_ID}"
        if detail_default
        else "/admin/billing/invoices"
    )
    assert (
        _return_url()(value, invoice_id=INVOICE_ID, detail_default=detail_default)
        == expected
    )


@pytest.mark.parametrize(
    "path", ["/admin/billing/invoices", f"/admin/billing/invoices/{INVOICE_ID}"]
)
def test_invoice_return_preserves_valid_query_and_fragment(path: str) -> None:
    value = path + "?status=issued&search=Example%20%26%20Sons&page=2#payments"
    assert _return_url()(value, invoice_id=INVOICE_ID) == value


@pytest.mark.parametrize("key", ["notice", "error"])
def test_invoice_return_adds_feedback_before_fragment(
    key: Literal["notice", "error"],
) -> None:
    target = _return_url()(
        "/admin/billing/invoices?status=issued&error=old#table",
        invoice_id=INVOICE_ID,
        message_key=key,
        message="Updated & sent",
    )
    parts = urlsplit(target)
    assert parts.path == "/admin/billing/invoices"
    assert parts.fragment == "table"
    assert parse_qs(parts.query) == {"status": ["issued"], key: ["Updated & sent"]}


def test_invoice_return_handlers_use_validation_on_every_redirect() -> None:
    tree = ast.parse(
        (ROOT / "app/web/admin/billing_invoice_actions.py").read_text(encoding="utf-8")
    )
    for name in ("invoice_send_and_return", "invoice_void_and_return"):
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        redirects = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "RedirectResponse"
        ]
        assert redirects
        for redirect in redirects:
            url = next(
                keyword.value for keyword in redirect.keywords if keyword.arg == "url"
            )
            assert isinstance(url, ast.Call)
            assert ast.unparse(url.func) == "_invoice_return_url"
