from pathlib import Path


def test_invoice_detail_status_badge_map_covers_terminal_and_partial_states():
    template = Path("templates/admin/billing/invoice_detail.html").read_text()

    assert "'issued': 'info'" in template
    assert "'partially_paid': 'warning'" in template
    assert "'void': 'inactive'" in template
    assert "'written_off': 'inactive'" in template
    assert "replace('_', ' ') | title" in template
    assert "status_variant_map.get(status_val, 'default')" in template


def test_invoice_edit_form_locks_currency_while_submitting_existing_value():
    template = Path("templates/admin/billing/invoice_form.html").read_text()

    assert (
        '<input type="hidden" name="currency" value="{{ invoice_currency }}">'
        in template
    )
    assert "name=\"{{ 'currency_display' if invoice else 'currency' }}\"" in template
    assert '{% if invoice %}disabled aria-disabled="true"{% endif %}' in template


def test_invoice_batch_run_button_has_submit_guard():
    template = Path("templates/admin/billing/invoice_batch.html").read_text()

    assert "@submit=\"if (submitting || !$refs.confirmed?.checked" in template
    assert "Run invoice batch for the previewed scope?" in template
    assert 'x-ref="confirmed"' in template
    assert ':disabled="!$refs.confirmed?.checked || submitting"' in template
    assert "submitting: false" in template
    assert "submitting ? 'Running...' : 'Run Batch'" in template


def test_billing_money_templates_render_currency_codes_not_naira_glyphs():
    template_paths = [
        "templates/admin/billing/invoice_detail.html",
        "templates/admin/billing/invoices.html",
        "templates/admin/billing/ledger.html",
        "templates/admin/billing/ar_aging.html",
    ]

    for template_path in template_paths:
        template = Path(template_path).read_text()
        assert "₦" not in template
        assert "currency" in template or "_display" in template
