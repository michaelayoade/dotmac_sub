from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoicePdfExport,
    InvoicePdfExportStatus,
    InvoiceStatus,
)
from app.services.web_billing_invoice_bulk import (
    BulkInvoiceActionResult,
    bulk_mark_paid,
    bulk_queue_pdf_exports,
    bulk_send,
    list_invoices_by_ids,
)
from app.services.web_billing_invoices import maybe_send_invoice_notification
from app.web.admin import billing_invoice_bulk as bulk_routes


def test_bulk_send_calls_invoice_notification_helper(
    db_session, subscriber, monkeypatch
):
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    called = {"count": 0}

    def _fake_send(db, *, invoice, send_notification):
        called["count"] += 1

    monkeypatch.setattr(
        "app.services.web_billing_invoice_bulk.web_billing_invoices_service.maybe_send_invoice_notification",
        _fake_send,
    )

    queued = bulk_send(db_session, str(invoice.id))

    assert queued == [str(invoice.id)]
    assert called["count"] == 1


def test_invoice_notification_email_includes_payment_summary_and_steps(
    db_session, subscriber, monkeypatch
):
    subscriber.account_number = "ACC-1001"
    subscriber.display_name = "Jane Customer"
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-1001",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("15000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("15000.00"),
        balance_due=Decimal("15000.00"),
        due_at=datetime(2026, 6, 24, tzinfo=UTC),
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)

    captured: dict[str, str] = {}

    def _fake_send_email(
        db, to_email, subject, body_html, body_text, activity, **kwargs
    ):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["body_html"] = body_html
        captured["body_text"] = body_text
        captured["activity"] = activity
        return True

    monkeypatch.setattr(
        "app.services.web_billing_invoices.email_service.send_email",
        _fake_send_email,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.email.send_email",
        _fake_send_email,
    )
    monkeypatch.setenv("APP_URL", "https://selfcare.dotmac.ng")

    maybe_send_invoice_notification(db_session, invoice=invoice, send_notification="1")

    assert captured["subject"] == "Invoice INV-1001 — payment due 2026-06-24"
    assert captured["activity"] == "billing_invoice"
    assert "Invoice Summary" in captured["body_html"]
    assert "ACC-1001" in captured["body_html"]
    assert "INV-1001" in captured["body_html"]
    assert "NGN 15,000.00" in captured["body_html"]
    assert "2026-06-24" in captured["body_html"]
    assert "How to pay through the portal" in captured["body_html"]
    assert "/portal/billing/pay?invoice=" in captured["body_html"]
    assert "Pay Invoice in Portal" in captured["body_html"]
    assert "1. Open the customer portal" in captured["body_text"]


def test_list_invoices_by_ids_preserves_order_and_deduplicates(db_session, subscriber):
    now = datetime.now(UTC)
    inv_a = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-BULK-A",
        status=InvoiceStatus.issued,
        subtotal=Decimal("50.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("50.00"),
        balance_due=Decimal("50.00"),
        created_at=now,
    )
    inv_b = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-BULK-B",
        status=InvoiceStatus.issued,
        subtotal=Decimal("75.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("75.00"),
        balance_due=Decimal("75.00"),
        created_at=now,
    )
    db_session.add_all([inv_a, inv_b])
    db_session.commit()

    ids_csv = f"{inv_b.id},missing-id,{inv_a.id},{inv_b.id}"
    invoices = list_invoices_by_ids(db_session, ids_csv)

    assert [invoice.id for invoice in invoices] == [inv_b.id, inv_a.id]


def test_bulk_mark_paid_updates_only_eligible_statuses(db_session, subscriber):
    now = datetime.now(UTC)
    inv_issued = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MP-ISSUED",
        status=InvoiceStatus.issued,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        created_at=now,
    )
    inv_overdue = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MP-OVERDUE",
        status=InvoiceStatus.overdue,
        subtotal=Decimal("80.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("80.00"),
        balance_due=Decimal("80.00"),
        created_at=now,
    )
    inv_draft = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-MP-DRAFT",
        status=InvoiceStatus.draft,
        subtotal=Decimal("40.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("40.00"),
        balance_due=Decimal("40.00"),
        created_at=now,
    )
    db_session.add_all([inv_issued, inv_overdue, inv_draft])
    db_session.commit()

    updated = bulk_mark_paid(
        db_session,
        f"{inv_issued.id},{inv_overdue.id},{inv_draft.id}",
    )
    db_session.refresh(inv_issued)
    db_session.refresh(inv_overdue)
    db_session.refresh(inv_draft)

    assert updated == [str(inv_issued.id), str(inv_overdue.id)]
    assert inv_issued.status == InvoiceStatus.paid
    assert inv_issued.balance_due == Decimal("0")
    assert inv_issued.paid_at is not None
    assert inv_overdue.status == InvoiceStatus.paid
    assert inv_overdue.balance_due == Decimal("0")
    assert inv_draft.status == InvoiceStatus.draft


def test_bulk_mark_paid_route_reports_skipped_count(db_session, monkeypatch):
    def _fake_execute_audited_bulk_action_result(
        db, request, *, action, invoice_ids_csv
    ):
        assert action == "mark_paid"
        assert invoice_ids_csv == "inv-1,inv-2,missing"
        return BulkInvoiceActionResult(
            selected_ids=["inv-1", "inv-2", "missing"],
            processed_ids=["inv-1"],
            skipped_ids=["inv-2", "missing"],
        )

    monkeypatch.setattr(
        bulk_routes.web_billing_invoice_bulk_service,
        "execute_audited_bulk_action_result",
        _fake_execute_audited_bulk_action_result,
    )

    response = bulk_routes.invoice_bulk_mark_paid(
        request=None,
        invoice_ids="inv-1,inv-2,missing",
        db=db_session,
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["count"] == 1
    assert payload["skipped"] == 2
    assert "2 skipped" in payload["message"]


def test_bulk_queue_pdf_exports_reports_ready_and_queued(
    db_session, subscriber, monkeypatch
):
    now = datetime.now(UTC)
    inv_ready = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PDF-READY",
        status=InvoiceStatus.issued,
        subtotal=Decimal("20.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("20.00"),
        balance_due=Decimal("20.00"),
        created_at=now,
    )
    inv_queue = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PDF-QUEUE",
        status=InvoiceStatus.issued,
        subtotal=Decimal("30.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("30.00"),
        balance_due=Decimal("30.00"),
        created_at=now,
    )
    db_session.add_all([inv_ready, inv_queue])
    db_session.commit()

    ready_export = InvoicePdfExport(
        invoice_id=inv_ready.id,
        status=InvoicePdfExportStatus.completed,
        file_path="fake/path/ready.pdf",
    )
    db_session.add(ready_export)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.web_billing_invoice_bulk.billing_invoice_pdf_service.export_file_exists",
        lambda db, export: bool(export and export.invoice_id == inv_ready.id),
    )
    queued_calls: list[str] = []

    def _fake_queue_export(db, invoice_id: str, requested_by_id: str | None = None):
        queued_calls.append(invoice_id)
        return None

    monkeypatch.setattr(
        "app.services.web_billing_invoice_bulk.billing_invoice_pdf_service.queue_export",
        _fake_queue_export,
    )

    result = bulk_queue_pdf_exports(
        db_session,
        f"{inv_ready.id},{inv_queue.id},missing-id",
        requested_by_id=str(subscriber.id),
    )

    assert result["ready"] == [str(inv_ready.id)]
    assert result["queued"] == [str(inv_queue.id)]
    assert "missing-id" in result["missing"]
    assert queued_calls == [str(inv_queue.id)]
