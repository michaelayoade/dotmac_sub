"""Vendors can see whether they have been paid.

The native purchase-invoice status only covers Sub-side review, so it never says
"paid". ERP is the payables/payment system of record; the owner maps its
``erp_purchase_invoice_status`` to a plain vendor-facing payment signal.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.schemas.status_presentation import StatusTone
from app.services.vendor_purchase_invoices import _payment_projection

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates/vendor/project_detail.html"
).read_text(encoding="utf-8")


def _inv(erp_status: str | None) -> SimpleNamespace:
    return SimpleNamespace(erp_purchase_invoice_status=erp_status)


def test_unsynced_invoice_reads_as_not_yet_sent_to_finance():
    projection = _payment_projection(_inv(None))
    assert projection["label"] == "Not yet sent to finance"
    assert projection["tone"] == StatusTone.neutral
    assert projection["detail"] is None


def test_paid_reads_as_paid():
    projection = _payment_projection(_inv("Paid"))
    assert projection["label"] == "Paid"
    assert projection["tone"] == StatusTone.positive
    assert projection["detail"] == "Paid"


def test_unpaid_does_not_read_as_paid_despite_the_substring():
    # "Unpaid" and "Partly Paid" both contain "paid" — order matters.
    assert _payment_projection(_inv("Unpaid"))["label"] == "Awaiting payment"
    assert _payment_projection(_inv("Unpaid"))["tone"] == StatusTone.warning
    assert _payment_projection(_inv("Partly Paid"))["label"] == "Partly paid"
    assert _payment_projection(_inv("Partly Paid"))["tone"] == StatusTone.info


def test_overdue_and_in_flight_states():
    assert _payment_projection(_inv("Overdue"))["label"] == "Payment overdue"
    assert _payment_projection(_inv("Overdue"))["tone"] == StatusTone.warning
    # Anything ERP reports that is not a paid/overdue state is still awaiting pay.
    assert _payment_projection(_inv("created"))["label"] == "Awaiting payment"
    assert _payment_projection(_inv("Submitted"))["label"] == "Awaiting payment"


def test_template_surfaces_the_payment_signal():
    assert "invoice.payment.label" in TEMPLATE
    assert "invoice.payment.tone" in TEMPLATE
    assert ">Payment</span>" in TEMPLATE
