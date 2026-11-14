"""New invoice interactions must compose shared UI and authoritative previews."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_catalogue_editor_reuses_shared_picker_and_review_modal() -> None:
    template = _read("templates/admin/billing/future_invoice_form.html")
    script = _read("static/js/future-invoice-authoring.js")
    assert 'data-typeahead-url="/admin/billing/future-invoice/catalogue"' in template
    assert "components/overlays/modal.html" in template
    assert "future-invoice-review-body" in template
    assert 'role="combobox"' not in template
    assert "confirmationOpen" not in script
    assert "Intl." not in script
    assert ".reduce(" not in script
    assert "unresolvedSelections.length" in script
    assert "validateReview()" in template


def test_financial_reviews_use_actual_action_form_renderer() -> None:
    for path in (
        "templates/admin/billing/_future_invoice_review.html",
        "templates/admin/billing/withholding_issue.html",
        "templates/admin/billing/withholding_correction.html",
        "templates/admin/billing/payment_create_confirm.html",
    ):
        template = _read(path)
        assert "components/forms/action_form.html" in template, path
        assert "{{ action_form(" in template, path
        assert "<form " not in template, path
        assert "onsubmit=" not in template, path
        assert ".format(" not in template, path


def test_withholding_display_uses_owner_status_and_formatted_values() -> None:
    template = _read("templates/billing/_invoice_withholding.html")
    assert "status_presentation_badge(invoice_withholding_display.status)" in template
    assert ".format(" not in template
    assert ".value.replace(" not in template
    payment_form = _read("templates/admin/billing/payment_form.html")
    assert "payment-withholding.js" not in payment_form
    assert "data-wht-toggle" not in payment_form


def test_shared_modal_uses_the_existing_focus_plugin_without_preventing_all_tabs() -> (
    None
):
    component = _read("templates/components/overlays/modal.html")
    assert 'x-trap.inert.noscroll="open"' in component
    assert "keydown.tab.prevent" not in component
    assert "alpine-focus.min.js" in _read("templates/base.html")
