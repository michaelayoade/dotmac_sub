from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_invoice_templates_consume_shared_status_presentation() -> None:
    paths = (
        "templates/admin/billing/invoices.html",
        "templates/admin/billing/_invoices_table.html",
        "templates/admin/billing/invoice_detail.html",
        "templates/admin/billing/index.html",
        "templates/admin/billing/account_detail.html",
        "templates/admin/customers/detail.html",
        "templates/admin/resellers/detail.html",
        "templates/admin/reports/invoices.html",
        "templates/customer/billing/index.html",
        "templates/customer/billing/invoice.html",
        "templates/reseller/accounts/invoices.html",
        "templates/reseller/accounts/invoice_detail.html",
    )

    for path in paths:
        source = _read(path)
        assert "status_presentation_badge" in source, path

    assert "status_variant_map" not in _read(
        "templates/admin/billing/invoice_detail.html"
    )
    assert "status_map =" not in _read("templates/admin/billing/invoices.html")


def test_customer_mobile_invoice_surfaces_render_server_presentation() -> None:
    status_chip = _read("mobile/lib/src/widgets/status_chip.dart")
    invoices = _read("mobile/lib/src/features/billing/invoices_screen.dart")
    detail = _read("mobile/lib/src/features/billing/invoice_detail_screen.dart")

    assert "StatusChip.forInvoice" not in status_chip
    assert "StatusChip.fromPresentation(inv.statusPresentation)" in invoices
    assert "StatusChip.fromPresentation(inv.statusPresentation)" in detail
    assert "inv.isOverdue ? 'overdue'" not in invoices
    assert "inv.isOverdue ? 'overdue'" not in detail
