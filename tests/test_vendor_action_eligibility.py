"""Vendor quote/invoice editability is owned by the serializer, not the template.

The vendor project detail used to decide whether the edit/submit forms showed by
re-deriving ``status in ['draft','revision_requested']`` in Jinja. The serializers
now expose an ``edit_action`` (the shared Action contract) from the same set the
mutation paths enforce — allowed when editable, otherwise blocked with a reason.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.vendor_portal_operations import _serialize_quote
from app.services.vendor_purchase_invoices import serialize as serialize_invoice


def _quote_row(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="q1",
        project_id="p1",
        vendor_id="v1",
        status=status,
        currency="NGN",
        subtotal=0,
        vat_rate_percent=0,
        tax_total=0,
        total=0,
        valid_from=None,
        valid_until=None,
        submitted_at=None,
        reviewed_at=None,
        review_notes=None,
        line_items=[],
    )


def _invoice_row(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="i1",
        project_id="p1",
        vendor_id="v1",
        invoice_number="INV-1",
        status=status,
        currency="NGN",
        tax_rate_percent=0,
        subtotal=0,
        tax_total=0,
        total=0,
        submitted_at=None,
        reviewed_at=None,
        reviewed_by_system_user_id=None,
        review_notes=None,
        created_by_system_user_id=None,
        attachment_stored_file_id=None,
        attachment=None,
        erp_purchase_order_id=None,
        erp_purchase_invoice_id=None,
        erp_purchase_invoice_status=None,
        erp_sync_error=None,
        erp_synced_at=None,
        erp_attachment_synced_at=None,
        is_active=True,
        created_at=None,
        updated_at=None,
        line_items=[],
    )


def test_quote_edit_action_only_allowed_for_editable_statuses():
    assert _serialize_quote(_quote_row("draft"))["edit_action"].allowed is True
    assert (
        _serialize_quote(_quote_row("revision_requested"))["edit_action"].allowed
        is True
    )
    approved = _serialize_quote(_quote_row("approved"))["edit_action"]
    assert approved.allowed is False
    assert approved.reason  # blocked actions carry a reason for the template
    assert _serialize_quote(_quote_row("submitted"))["edit_action"].allowed is False


def test_invoice_edit_action_only_allowed_for_editable_statuses():
    assert serialize_invoice(_invoice_row("draft"))["edit_action"].allowed is True
    assert (
        serialize_invoice(_invoice_row("revision_requested"))["edit_action"].allowed
        is True
    )
    approved = serialize_invoice(_invoice_row("approved"))["edit_action"]
    assert approved.allowed is False
    assert approved.reason
    assert serialize_invoice(_invoice_row("submitted"))["edit_action"].allowed is False
