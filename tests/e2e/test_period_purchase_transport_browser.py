"""Real-browser transport acceptance; gateway responses are explicit test doubles."""

import json
import os
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from jinja2 import Environment
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope="module")
def checkout_browser():
    source = Path("templates/customer/billing/service_periods.html").read_text(
        encoding="utf-8"
    )
    body = source.split("{% block content %}", 1)[1].rsplit("{% endblock %}", 1)[0]
    base = Path("templates/base.html").read_text(encoding="utf-8")
    helper = base[
        base.index("function getCsrfToken()") : base.index("// Configure HTMX")
    ]
    html = (
        Environment(autoescape=True)
        .from_string(body)
        .render(
            max_periods=12,
            subscriptions=[
                SimpleNamespace(
                    id="00000000-0000-0000-0000-000000000001",
                    offer=SimpleNamespace(name="Reviewed monthly service"),
                )
            ],
            payment_options=[
                SimpleNamespace(provider_type="paystack", label="Paystack")
            ],
            saved_cards=[
                SimpleNamespace(
                    id="00000000-0000-0000-0000-000000000002", label="Test saved card"
                )
            ],
        )
    )
    document = (
        '<html><head><style>.hidden{display:none}</style><meta name="csrf-token" content="browser-token"><script>'
        + helper
        + "</script></head><body>"
        + html
        + "</body></html>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(document.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(
                channel="chrome" if os.name == "nt" else None, headless=True
            )
            try:
                yield browser, f"http://127.0.0.1:{server.server_port}"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _quote():
    now = datetime.now(UTC)
    start, end = now + timedelta(days=2), now + timedelta(days=33)
    return {
        "subtotal": "100.00",
        "tax_total": "7.50",
        "total": "107.50",
        "currency": "NGN",
        "coverage_starts_at": start.isoformat(),
        "coverage_ends_at": end.isoformat(),
        "timezone": "Africa/Lagos",
        "preview_fingerprint": "a" * 64,
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "periods": [
            {
                "ordinal": 1,
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
                "tax_total": "7.50",
                "total": "107.50",
            }
        ],
    }


def _page(checkout_browser):
    browser, origin = checkout_browser
    context = browser.new_context()
    context.add_cookies(
        [{"name": "csrf_token", "value": "browser-token", "url": origin}]
    )
    page = context.new_page()
    page.goto(origin + "/portal/billing/service-periods")
    return context, page, origin


def test_browser_review_dates_and_csrf(checkout_browser):
    context, page, _ = _page(checkout_browser)
    try:
        requests = []

        def preview(route):
            requests.append(route.request.headers)
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(_quote())
            )

        page.route("**/portal/billing/service-periods/preview", preview)
        page.locator("#preview-button").click()
        expect(page.locator("#preview-coverage")).to_contain_text("Coverage:")
        expect(page.locator("#preview-periods")).to_contain_text("Period 1:")
        expect(page.locator("#preview-expiry")).to_contain_text("expires")
        assert requests[0]["x-csrf-token"] == "browser-token"
        assert "Africa/Lagos" in page.locator("#preview-coverage").inner_text()
    finally:
        context.close()


def test_browser_html_rejection_is_visible(checkout_browser):
    context, page, _ = _page(checkout_browser)
    try:
        page.route(
            "**/portal/billing/service-periods/preview",
            lambda route: route.fulfill(
                status=403, content_type="text/html", body="<h1>Session expired</h1>"
            ),
        )
        page.locator("#preview-button").click()
        expect(page.locator("#period-error")).to_contain_text("Refresh")
        expect(page.locator("#preview-button")).to_be_enabled()
    finally:
        context.close()


def test_browser_double_click_and_unknown_retry_keep_one_key(checkout_browser):
    context, page, _ = _page(checkout_browser)
    try:
        page.route(
            "**/portal/billing/service-periods/preview",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(_quote())
            ),
        )
        keys = []

        def unknown(route):
            assert route.request.headers["x-csrf-token"] == "browser-token"
            keys.append(route.request.headers["idempotency-key"])
            route.abort("failed")

        page.route("**/portal/billing/service-periods/intent", unknown)
        page.locator("#preview-button").click()
        expect(page.locator("#pay-button")).to_be_visible()
        page.evaluate(
            "document.getElementById('pay-button').click(); document.getElementById('pay-button').click()"
        )
        expect(page.locator("#period-error")).to_contain_text("existing payment")
        assert len(keys) == 1
        page.locator("#pay-button").click()
        page.wait_for_function(
            "document.getElementById('pay-button').disabled === false"
        )
        assert len(keys) == 2 and keys[0] == keys[1]
    finally:
        context.close()


def test_browser_stale_preview_cannot_enable_payment(checkout_browser):
    context, page, _ = _page(checkout_browser)
    try:
        page.evaluate(
            "() => { window.fetch = () => new Promise(resolve => { window.releaseQuote = resolve; }); }"
        )
        page.locator("#preview-button").click()
        page.locator("#period-count").fill("2")
        page.locator("#period-count").dispatch_event("change")
        page.evaluate(
            "data => window.releaseQuote(new Response(JSON.stringify(data), {status:200}))",
            _quote(),
        )
        expect(page.locator("#pay-button")).to_be_hidden()
        expect(page.locator("#preview-button")).to_be_enabled()
    finally:
        context.close()
