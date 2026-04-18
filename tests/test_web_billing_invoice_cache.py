from __future__ import annotations

from decimal import Decimal

from app.models.billing import InvoicePdfExport, InvoicePdfExportStatus
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services import web_billing_invoice_cache as cache_service


def test_build_cache_page_state_loads_accounts_without_distinct_subscriber_rows(
    db_session, subscriber_account
):
    subscriber_account.metadata = {"source": "log-regression", "tags": ["json"]}
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="NGN",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.completed,
        requested_by_id=subscriber_account.id,
        file_path="generated_docs/public/invoice_pdf_export/test/file.pdf",
        file_size_bytes=2048,
    )
    db_session.add(export)
    db_session.commit()

    state = cache_service.build_cache_page_state(
        db_session,
        date_from=None,
        date_to=None,
        account_id=None,
    )

    assert state["accounts"]
    assert str(subscriber_account.id) in {
        str(account.id) for account in state["accounts"]
    }
