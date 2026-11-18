"""Production shared-picker/modal smoke test; owner evidence and layout are doubles."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from app.services.action_forms import ActionConfirmation, ActionForm, ActionHiddenValue

root = Path(__file__).resolve().parents[2]
layout = """<!doctype html><html><head><style>[x-cloak],.hidden{display:none!important}</style><script defer src="/static/js/vendor/htmx.min.js"></script></head><body>{% block content %}{% endblock %}{% block scripts %}{% endblock %}<script src="/static/js/typeahead.js"></script><script src="/static/js/vendor/alpine-focus.min.js"></script><script src="/static/js/vendor/alpine.min.js"></script></body></html>"""
env = Environment(
    autoescape=True,
    loader=ChoiceLoader(
        [
            DictLoader(
                {"layouts/admin.html": layout, "components/forms/csrf_input.html": ""}
            ),
            FileSystemLoader(root / "templates"),
        ]
    ),
)
html = env.get_template("admin/billing/future_invoice_form.html").render(
    invoice=None,
    error=None,
    account_locked=True,
    account_label="Example customer",
    selected_account_id="account",
    invoice_config={"accountId": "account", "currency": "NGN"},
    currency_options=("NGN",),
    action_url="/save",
    review_url="/review",
    draft_idempotency_key="stable-draft-key",
    default_due_date="2027-01-01",
)
line = {
    "description": "Router replacement",
    "quantity": "2",
    "unit_price": "1000",
    "tax_total": "150",
    "total": "2150",
    "total_display": "NGN 2,150.00",
    "tax_total_display": "NGN 150.00",
    "period_kind": "one_time",
    "subscription_id": None,
    "starts_at": None,
    "selection": {
        "query": {"item_kind": "add_on", "item_id": "router"},
        "fingerprint": "owner-fingerprint",
    },
}
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=os.environ["FUTURE_INVOICE_CHROMIUM"], args=["--no-sandbox"]
    )
    page = browser.new_page(viewport={"width": 390, "height": 844})
    errors, submissions = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(request):
        path = request.request.url.split("http://localhost")[1].split("?")[0]
        if path.startswith("/static/"):
            request.fulfill(
                body=(root / path.lstrip("/")).read_bytes(),
                content_type="application/javascript",
            )
        elif path == "/":
            request.fulfill(body=html, content_type="text/html")
        elif path == "/review":
            values = parse_qs(request.request.post_data)
            action = ActionForm(
                key="fixture.invoice_review",
                fields=(),
                title="Confirm invoice for Example customer",
                description="Check the selected catalogue items.",
                action_url="/save",
                submit_label="Save confirmed draft",
                confirmation=ActionConfirmation(
                    "I have checked these invoice lines",
                    "Saving reserves the selected periods.",
                ),
                hidden_values=tuple(
                    ActionHiddenValue(key, values[key][0])
                    for key in ("line_items_json", "draft_idempotency_key")
                ),
            )
            review = SimpleNamespace(lines=(SimpleNamespace(**line),), action=action)
            request.fulfill(
                body=env.get_template(
                    "admin/billing/_future_invoice_review.html"
                ).render(review=review),
                content_type="text/html",
                headers={
                    "HX-Trigger": '{"open-modal":{"name":"future-invoice-review"}}'
                },
            )
        elif path == "/save":
            submissions.append(parse_qs(request.request.post_data))
            request.fulfill(body="Saved fixture", content_type="text/html")
        elif path.endswith("/catalogue"):
            request.fulfill(
                body=json.dumps(
                    {
                        "items": [
                            {
                                "id": "router-price",
                                "label": "Router replacement · NGN 1,000.00",
                                "item_kind": "add_on",
                                "item_id": "router",
                                "price_id": "price",
                                "name": "Router replacement",
                                "price_type": "one_time",
                                "amount": "1000",
                            }
                        ]
                    }
                ),
                content_type="application/json",
            )
        elif path.endswith("/preview"):
            request.fulfill(body=json.dumps(line), content_type="application/json")
        else:
            request.fulfill(body="{}", content_type="application/json")

    page.route("**/*", route)
    page.goto("http://localhost/")
    page.locator("#future-catalogue").fill("router")
    page.locator("[data-typeahead-results] [role=option]").wait_for()
    page.locator("#future-catalogue").press("ArrowDown")
    assert page.locator("[data-typeahead-results] [role=option]").evaluate(
        "option => option === document.activeElement"
    )
    page.keyboard.press("Enter")
    page.locator("#future-quantity").fill("2")
    page.get_by_role("button", name="Add invoice line").click()
    page.wait_for_function(
        "!Alpine.$data(document.querySelector('[data-customer-label]')).busy",
        timeout=5000,
    )
    added = page.evaluate(
        """() => { const ui=Alpine.$data(document.querySelector('[data-customer-label]')); return {line_count:ui.lines.length,error:ui.error,busy:ui.busy,restoring:ui.restoring,submitting:ui.submitting,selected_item:ui.selectedItem}; }"""
    )
    assert added["line_count"] == 1, {"controller": added, "page_errors": errors}
    assert not errors, errors
    page.get_by_role("button", name="Review and save draft").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible")
    assert "Quantity 2" in dialog.inner_text()
    assert not dialog.locator("[name=confirmed]").is_checked()
    dialog.get_by_role("button", name="Save confirmed draft").click()
    assert not submissions
    dialog.locator("[name=confirmed]").check()
    dialog.get_by_role("button", name="Save confirmed draft").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.closest('[role=dialog]') !== null")
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    # Actual start dates and reservation references are read projections, never calendar-month guesses.
    page.evaluate(
        """() => { const ui=Alpine.$data(document.querySelector('[data-customer-label]'));ui.lines=[];ui.selectedItem={name:'Unlimited Elite',price_type:'recurring'};ui.subscriptions=[{subscription_id:'sub',label:'Unlimited Elite'}];ui.subscriptionId='sub';ui.periods=[{period_kind:'monthly',starts_at:'2030-01-02T00:00:00Z',starts_on:'2030-01-02',ends_at:'2030-02-02T00:00:00Z',label:'2 Jan 2030 to 2 Feb 2030',available:true},{period_kind:'monthly',starts_at:'2030-02-02T00:00:00Z',starts_on:'2030-02-02',ends_at:'2030-03-02T00:00:00Z',label:'2 Feb 2030 to 2 Mar 2030',available:false,reason:'Reserved by invoice 1125',conflict_invoice_id:'1125'}]; }"""
    )
    page.locator("#future-calendar").fill("2030-01-02")
    page.locator("#future-calendar").dispatch_event("change")
    assert page.locator("input[name=period_choice]").first.is_checked()
    assert page.locator("input[name=period_choice]").nth(1).is_disabled()
    assert (
        page.get_by_role("link", name="View existing invoice")
        .last.get_attribute("href")
        .endswith("/1125")
    )
    page.evaluate(
        """() => { const ui=Alpine.$data(document.querySelector('[data-customer-label]'));ui.periods=[{period_kind:'capped_pending_renewal',available:true,label:'100 GB, valid 30 days after payment'}]; }"""
    )
    page.locator("#future-calendar").wait_for(state="hidden")
    page.get_by_text("100 GB, valid 30 days after payment").last.wait_for(
        state="visible"
    )
    # Confirm the actual shared renderer carries the owner selections through its final native POST.
    page.evaluate(
        """line => { const ui=Alpine.$data(document.querySelector('[data-customer-label]'));ui.lines=[{...line,clientId:'final-line'}]; }""",
        line,
    )
    page.get_by_role("button", name="Review and save draft").click()
    dialog.wait_for(state="visible")
    dialog.locator("[name=confirmed]").check()
    dialog.get_by_role("button", name="Save confirmed draft").click()
    page.wait_for_url("http://localhost/save")
    assert submissions[0]["confirmed"] == ["yes"]
    assert (
        json.loads(submissions[0]["line_items_json"][0])[0]["catalogue_selection"][
            "fingerprint"
        ]
        == "owner-fingerprint"
    )
    assert not errors, errors
    print(
        "Browser PASS: shared typeahead, modal, required confirmation/focus trap, authoritative hidden payload, actual dates, reservations and capped renewal display; no JS errors"
    )
    browser.close()
