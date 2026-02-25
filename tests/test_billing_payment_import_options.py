from __future__ import annotations

from app.models.audit import AuditActorType
from app.models.billing import Payment
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.web_billing_payments import (
    import_payments,
    list_payment_import_history,
    list_payment_import_history_filtered,
    normalize_import_rows,
    render_payment_import_history_csv,
)


def test_import_payments_applies_payment_source_to_memo(db_session, subscriber):
    subscriber.account_number = "ACC-IMPORT-1"
    db_session.add(subscriber)
    db_session.commit()

    imported, errors = import_payments(
        db_session,
        [{"account_number": "ACC-IMPORT-1", "amount": "1500", "reference": "TRF-100"}],
        "NGN",
        payment_source="Zenith 461 Bank",
        pair_inactive_customers=True,
    )

    assert imported == 1
    assert errors == []
    payment = db_session.query(Payment).order_by(Payment.created_at.desc()).first()
    assert payment is not None
    assert "[Zenith 461 Bank]" in (payment.memo or "")
    assert "TRF-100" in (payment.memo or "")


def test_import_payments_respects_pair_inactive_customers_flag(db_session, subscriber):
    subscriber.account_number = "ACC-INACTIVE-1"
    subscriber.is_active = False
    db_session.add(subscriber)
    db_session.commit()

    imported, errors = import_payments(
        db_session,
        [{"account_number": "ACC-INACTIVE-1", "amount": "900"}],
        "NGN",
        pair_inactive_customers=False,
    )

    assert imported == 0
    assert len(errors) == 1
    assert "Account not found" in errors[0]


def test_normalize_import_rows_maps_bank_specific_columns():
    rows = [
        {
            "acct_no": "ACC-01",
            "credit_amount": "5000",
            "value_date": "2026-02-01",
            "narration": "REV-1",
        }
    ]
    normalized = normalize_import_rows(rows, "zenith_bank")

    assert normalized[0]["account_number"] == "ACC-01"
    assert normalized[0]["amount"] == "5000"
    assert normalized[0]["date"] == "2026-02-01"
    assert normalized[0]["reference"] == "REV-1"


def test_normalize_import_rows_supports_fixed_width_handler():
    rows = [
        {
            "account_number": "ACC-FW-01",
            "amount": "4500",
            "currency": "NGN",
            "reference": "FW-REF-01",
            "date": "2026-02-10",
        }
    ]

    normalized = normalize_import_rows(rows, "fixed_width_basic")

    assert normalized[0]["account_number"] == "ACC-FW-01"
    assert normalized[0]["amount"] == "4500"
    assert normalized[0]["currency"] == "NGN"
    assert normalized[0]["reference"] == "FW-REF-01"


def test_import_payments_assigns_selected_payment_method_type(db_session, subscriber):
    subscriber.account_number = "ACC-METHOD-1"
    db_session.add(subscriber)
    db_session.commit()

    imported, errors = import_payments(
        db_session,
        [{"account_number": "ACC-METHOD-1", "amount": "700", "reference": "TRF-700"}],
        "NGN",
        payment_method_type="transfer",
        pair_inactive_customers=True,
    )

    assert imported == 1
    assert errors == []
    payment = db_session.query(Payment).order_by(Payment.created_at.desc()).first()
    assert payment is not None
    assert payment.payment_method is not None
    assert payment.payment_method.method_type.value == "transfer"


def test_list_payment_import_history_reads_audit_metadata(db_session):
    audit_service.audit_events.create(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            metadata_={
                "file_name": "bank.csv",
                "handler": "zenith_bank",
                "payment_method_type": "transfer",
                "row_count": 3,
                "imported": 2,
                "errors": 1,
                "total_amount": 1250.0,
            },
        ),
    )

    rows = list_payment_import_history(db_session, limit=5)

    assert len(rows) >= 1
    first = rows[0]
    assert first["file_name"] == "bank.csv"
    assert first["handler"] == "zenith_bank"
    assert first["payment_method_type"] == "transfer"
    assert first["status"] == "partial"
    assert first["row_count"] == 3
    assert first["matched_count"] == 2
    assert first["unmatched_count"] == 1
    assert first["total_amount"] == 1250.0


def test_list_payment_import_history_filtered_by_status_and_handler(db_session):
    audit_service.audit_events.create(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            metadata_={"handler": "base_csv", "imported": 1, "errors": 0},
        ),
    )
    audit_service.audit_events.create(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            metadata_={"handler": "gtbank", "imported": 0, "errors": 2},
        ),
    )

    rows = list_payment_import_history_filtered(
        db_session,
        limit=10,
        handler="gtbank",
        status="failed",
        date_range=None,
    )

    assert len(rows) == 1
    assert rows[0]["handler"] == "gtbank"
    assert rows[0]["status"] == "failed"


def test_render_payment_import_history_csv_contains_headers_and_rows():
    csv_text = render_payment_import_history_csv(
        [
            {
                "occurred_at": None,
                "file_name": "payments.csv",
                "handler": "base_csv",
                "status": "success",
                "payment_source": "Zenith",
                "payment_method_type": "transfer",
                "row_count": 4,
                "matched_count": 4,
                "unmatched_count": 0,
                "total_amount": 5000.5,
            }
        ]
    )

    assert "occurred_at,file_name,handler,status,payment_source,payment_method_type,row_count,matched_count,unmatched_count,total_amount" in csv_text
    assert "payments.csv,base_csv,success,Zenith,transfer,4,4,0,5000.50" in csv_text
