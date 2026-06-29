from pathlib import Path


def test_invoice_detail_status_badge_map_covers_terminal_and_partial_states():
    template = Path("templates/admin/billing/invoice_detail.html").read_text()

    assert "'issued': 'info'" in template
    assert "'partially_paid': 'warning'" in template
    assert "'void': 'inactive'" in template
    assert "'written_off': 'inactive'" in template
    assert "replace('_', ' ') | title" in template
    assert "status_variant_map.get(status_val, 'default')" in template
