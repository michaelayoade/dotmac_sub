from app.services.notification_template_renderer import (
    default_preview_variables,
    render_template_text,
)


def test_render_template_text_supports_brace_styles():
    rendered = render_template_text(
        "Hello {{customer_name}}. Invoice {invoice_number} due {due_date}.",
        {
            "customer_name": "Ada",
            "invoice_number": "INV-1",
            "due_date": "2026-03-01",
        },
    )
    assert rendered == "Hello Ada. Invoice INV-1 due 2026-03-01."


def test_render_template_text_keeps_unknown_variables():
    rendered = render_template_text("Hi {{known}} {{unknown}}", {"known": "x"})
    assert rendered == "Hi x {{unknown}}"


def test_default_preview_variables_contains_expected_keys():
    values = default_preview_variables()
    assert "customer_name" in values
    assert "amount_due" in values
