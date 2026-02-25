from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.audit import AuditActorType
from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.web_billing_invoice_batch import (
    get_billing_run_schedule,
    save_billing_run_schedule,
)
from app.services.web_billing_invoices import (
    apply_proforma_form_values,
    convert_proforma_to_final,
)
from app.services.web_billing_reconciliation import build_reconciliation_data


def test_apply_proforma_form_values_marks_and_cleans():
    number, memo = apply_proforma_form_values(
        invoice_number="INV-100",
        memo="Initial note",
        proforma_invoice=True,
    )
    assert number == "PF-INV-100"
    assert memo is not None
    assert "[PROFORMA]" in memo

    clean_number, clean_memo = apply_proforma_form_values(
        invoice_number=number,
        memo=memo,
        proforma_invoice=False,
    )
    assert clean_number == "INV-100"
    assert clean_memo == "Initial note"


def test_convert_proforma_to_final_updates_status_and_clears_marker(db_session, subscriber):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="PF-INV-200",
        status=InvoiceStatus.draft,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        memo="[PROFORMA] Draft quote",
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)

    converted = convert_proforma_to_final(db_session, invoice_id=str(invoice.id))

    assert converted.status == InvoiceStatus.issued
    assert "[PROFORMA]" not in (converted.memo or "")
    assert not (converted.invoice_number or "").startswith("PF-")


def test_billing_run_schedule_can_be_saved_and_loaded(db_session):
    saved = save_billing_run_schedule(
        db_session,
        enabled=True,
        run_day="5",
        run_time="03:30",
        timezone="Africa/Lagos",
        billing_cycle="monthly",
        partner_ids=[],
    )
    loaded = get_billing_run_schedule(db_session)

    assert saved["enabled"] is True
    assert loaded["run_day"] == 5
    assert loaded["run_time"] == "03:30"
    assert loaded["timezone"] == "Africa/Lagos"


def test_reconciliation_builds_unmatched_and_duplicate_views(db_session, subscriber):
    payment_a = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        status=PaymentStatus.succeeded,
        external_id="TRX-1",
        created_at=datetime.now(UTC),
    )
    payment_b = Payment(
        account_id=subscriber.id,
        amount=Decimal("120.00"),
        status=PaymentStatus.succeeded,
        external_id="TRX-1",
        created_at=datetime.now(UTC),
    )
    db_session.add_all([payment_a, payment_b])
    db_session.commit()

    audit_service.audit_events.create(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            metadata_={
                "handler": "base_csv",
                "file_name": "bank.csv",
                "row_count": 5,
                "imported": 3,
                "errors": 2,
                "total_amount": 500.0,
            },
        ),
    )

    state = build_reconciliation_data(db_session, date_range=None, handler="base_csv")

    assert state["summary"]["unmatched_rows"] == 2
    assert len(state["duplicate_candidates"]) == 1
    assert state["duplicate_candidates"][0]["external_id"] == "TRX-1"
