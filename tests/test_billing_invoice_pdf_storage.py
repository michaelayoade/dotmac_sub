from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.models.billing import InvoicePdfExport, InvoicePdfExportStatus
from app.models.stored_file import StoredFile
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as pdf_service
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload(self, key: str, data: bytes, content_type: str | None):
        self.objects[key] = data

    def delete(self, key: str):
        self.objects.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.objects

    def stream(self, key: str) -> StreamResult:
        payload = self.objects[key]
        return StreamResult(iter([payload]), "application/pdf", len(payload))


def _invoice(db_session, subscriber_account):
    return billing_service.invoices.create(
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


def test_process_export_uploads_invoice_pdf_to_s3_metadata(
    db_session, subscriber_account, monkeypatch
):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)
    SessionLocal = sessionmaker(
        bind=db_session.get_bind(), autoflush=False, autocommit=False
    )
    monkeypatch.setattr(pdf_service, "SessionLocal", SessionLocal)
    monkeypatch.setattr(pdf_service, "_build_pdf_bytes", lambda _invoice: b"%PDF-1.4 bytes")

    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=subscriber_account.id,
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)

    result = pdf_service.process_export(str(export.id))
    db_session.expire_all()
    export = db_session.get(InvoicePdfExport, export.id)
    assert export is not None

    assert result["status"] == "completed"
    assert export.status == InvoicePdfExportStatus.completed
    assert export.file_path is not None
    assert export.file_path in fake_storage.objects
    assert export.file_size_bytes == len(b"%PDF-1.4 bytes")
    record = (
        db_session.query(StoredFile)
        .filter(StoredFile.entity_type == "invoice_pdf_export")
        .filter(StoredFile.entity_id == str(export.id))
        .filter(StoredFile.is_deleted.is_(False))
        .first()
    )
    assert record is not None


def test_export_file_exists_and_stream_export_uses_s3(
    db_session, subscriber_account, monkeypatch
):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)

    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.completed,
        requested_by_id=subscriber_account.id,
        file_path="generated_docs/public/invoice_pdf_export/abc/file.pdf",
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)

    file_uploads.upload(
        db=db_session,
        domain="generated_docs",
        entity_type="invoice_pdf_export",
        entity_id=str(export.id),
        original_filename="invoice-test.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 body",
        uploaded_by=str(subscriber_account.id),
        organization_id=None,
    )
    current = file_uploads.get_active_entity_file(
        db_session, "invoice_pdf_export", str(export.id)
    )
    assert current is not None
    export.file_path = current.storage_key_or_relative_path
    db_session.commit()
    db_session.refresh(export)

    assert pdf_service.export_file_exists(db_session, export) is True
    stream = pdf_service.stream_export(db_session, export)
    assert b"".join(stream.chunks) == b"%PDF-1.4 body"
