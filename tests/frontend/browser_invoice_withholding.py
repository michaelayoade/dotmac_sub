"""Production WHT templates/JS smoke test with mocked owner API responses.

Run with Playwright installed and FUTURE_INVOICE_CHROMIUM set to Chromium.
The layout shell, owner responses and submission destinations are test doubles.
"""

import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionHiddenValue,
)

root = Path(__file__).resolve().parents[2]
layout = "<!doctype html><html><head><style>[hidden]{display:none!important}</style></head><body>{% block content %}{% endblock %}{% block scripts %}{% endblock %}</body></html>"
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
account = "11111111-1111-1111-1111-111111111111"
invoice = "22222222-2222-2222-2222-222222222222"
issue_action = ActionForm(
    key="invoice.withholding_issue",
    title="Confirm invoice issue",
    description="Use existing cash and record withholding separately.",
    action_url=f"/admin/billing/invoices/{invoice}/issue",
    submit_label="Issue invoice",
    fields=(
        ActionField(
            "evidence_reference",
            "WHT claim / supporting evidence reference",
            ActionFieldKind.text,
            required=True,
        ),
    ),
    impact="Cash credit NGN 95.00; withholding NGN 5.00; invoice total NGN 100.00.",
    confirmation=ActionConfirmation(
        "I reviewed the invoice and withholding evidence",
        "The certificate remains pending.",
    ),
    hidden_values=(
        ActionHiddenValue("preview_fingerprint", "d" * 64),
        ActionHiddenValue("idempotency_token", "stable-issue-token"),
    ),
)
issued = env.get_template("admin/billing/withholding_issue.html").render(
    invoice_id=invoice,
    invoice_label="Invoice 1125",
    account_label="Example customer",
    issue_action_form=issue_action,
)
env.filters["app_datetime"] = lambda value, *args, **kwargs: str(value or "")
source = (root / "templates/admin/billing/invoice_detail.html").read_text()
start = source.index('<form id="invoice-issue-form"')
issue_form = env.from_string(
    source[start : source.index("</form>", start) + len("</form>")]
)
state = {"selected": False, "version": 0, "status": "draft"}


def tax_panel():
    model = NS(
        id=invoice,
        status=NS(value=state["status"]),
        currency="NGN",
        subtotal=100,
        tax_total=0,
        total=100,
        discount_amount=0,
    )
    return env.get_template("admin/billing/_invoice_tax_details.html").render(
        invoice=model,
        invoice_withholding_selection=NS(
            eligible=state["status"] == "draft",
            editable=state["status"] == "draft",
            selected=state["selected"],
            version=state["version"],
        ),
        invoice_withholding=NS(
            expected_wht_amount=Decimal("5") if state["selected"] else None,
            expected_net_amount=Decimal("95") if state["selected"] else None,
        ),
        invoice_tax_display=NS(
            subtotal="NGN 100.00", vat="NGN 0.00", total="NGN 100.00"
        ),
        invoice_withholding_display=NS(
            expected_wht_amount="NGN 5.00", expected_net_amount="NGN 95.00"
        ),
        can_select_invoice_withholding=True,
    )


def detail_page():
    control = (
        issue_form.render(invoice=NS(id=invoice)) if state["status"] == "draft" else ""
    )
    return (
        '<!doctype html><html><head><script defer src="/static/js/vendor/htmx.min.js"></script><style>.htmx-indicator{display:none}</style></head><body>'
        + control
        + tax_panel()
        + "</body></html>"
    )


with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=os.environ["FUTURE_INVOICE_CHROMIUM"], args=["--no-sandbox"]
    )
    page = browser.new_page(viewport={"width": 390, "height": 844})
    errors = []
    submissions = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(request):
        path = request.request.url.split("http://localhost")[1].split("?")[0]
        if path.startswith("/static/"):
            request.fulfill(
                body=(root / path.lstrip("/")).read_bytes(),
                content_type="application/javascript",
            )
        elif path.endswith("/withholding-selection"):
            if state.get("fail_selection"):
                request.abort("failed")
                return
            data = parse_qs(request.request.post_data)
            assert int(data["expected_version"][0]) == state["version"]
            state["selected"] = data.get("selected") == ["true"]
            state["version"] += 1
            request.fulfill(body=tax_panel(), content_type="text/html")
        elif path.endswith("/issue"):
            data = parse_qs(request.request.post_data)
            submissions.append(data)
            assert state["selected"]
            if "preview_fingerprint" in data:
                state["status"] = "issued"
                request.fulfill(body=detail_page(), content_type="text/html")
            else:
                request.fulfill(body=issued, content_type="text/html")
        elif path.endswith("/detail"):
            request.fulfill(body=detail_page(), content_type="text/html")
        else:
            request.fulfill(body=detail_page(), content_type="text/html")

    page.route("**/*", route)
    page.goto("http://localhost/detail")
    checkbox = page.locator("#invoice-wht-selected")
    assert not checkbox.is_checked()
    assert page.get_by_role("button", name="Issue invoice", exact=True).count() == 1
    assert "issue-withholding" not in page.content()
    checkbox.check()
    page.wait_for_function(
        "document.querySelector('[name=expected_version]').value === '1'"
    )
    assert page.get_by_text("Net cash required").is_visible()
    checkbox.uncheck()
    page.wait_for_function(
        "document.querySelector('[name=expected_version]').value === '2'"
    )
    assert page.get_by_text("Net cash required").count() == 0
    checkbox.check()
    page.wait_for_function(
        "document.querySelector('[name=expected_version]').value === '3'"
    )
    page.get_by_role("button", name="Issue invoice", exact=True).click()
    page.get_by_role("heading", name="Confirm invoice issue").first.wait_for()
    assert "Cash credit NGN 95.00" in page.locator("body").inner_text()
    assert "withholding NGN 5.00" in page.locator("body").inner_text()
    page.locator("[name=evidence_reference]").fill("WHT-ISSUE-DECLARATION")
    assert not page.locator("[name=confirmed]").is_checked()
    page.locator("[name=confirmed]").check()
    page.get_by_role("button", name="Issue invoice", exact=True).click()
    page.get_by_text(
        "The WHT selection is locked because this invoice has been issued."
    ).wait_for()
    assert page.locator("#invoice-wht-selected").is_checked()
    assert page.locator("#invoice-wht-selected").is_disabled()
    assert submissions[-1]["preview_fingerprint"] == ["d" * 64]
    assert submissions[-1]["confirmed"] == ["yes"]
    state.update(selected=False, version=0, status="draft", fail_selection=True)
    page.goto("http://localhost/detail")
    page.locator("#invoice-wht-selected").check()
    page.get_by_text(
        "WHT selection was not saved. Reload this invoice before issuing it."
    ).wait_for()
    page.wait_for_function("document.getElementById('invoice-issue-button').disabled")
    assert not state["selected"]
    assert not errors, errors
    print(
        "PASS: VAT-side WHT checkbox defaults unchecked, updates owner breakdown, remains editable in draft, uses the existing Issue invoice action, and is checked/disabled after issue; no JS errors."
    )
    browser.close()
