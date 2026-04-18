from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.models.billing import InvoicePdfExport, InvoicePdfExportStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.stored_file import StoredFile
from app.models.subscription_engine import SettingValueType
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
    monkeypatch.setattr(
        pdf_service, "_build_pdf_bytes", lambda _db, _invoice: b"%PDF-1.4 bytes"
    )

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
        owner_subscriber_id=None,
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


def test_queue_export_ignores_non_subscriber_requested_by_id(
    db_session, subscriber_account, monkeypatch
):
    invoice = _invoice(db_session, subscriber_account)

    captured: dict[str, object] = {}

    def _fake_enqueue(
        task,
        *,
        args=None,
        kwargs=None,
        correlation_id=None,
        source=None,
        actor_id=None,
        **extra,
    ):
        captured["task"] = task
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["correlation_id"] = correlation_id
        captured["source"] = source
        captured["actor_id"] = actor_id
        captured["extra"] = extra
        return type("AsyncResult", (), {"id": "task-123"})()

    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    export = pdf_service.queue_export(
        db_session,
        invoice_id=str(invoice.id),
        requested_by_id="87bdb5e4-d626-4541-9aff-25b04b0423a4",
    )

    assert export.requested_by_id is None
    assert captured["args"] == [str(export.id)]
    assert captured["kwargs"] is None
    assert captured["correlation_id"] == f"invoice_pdf_export:{export.id}"
    assert captured["source"] == "billing_invoice_pdf"
    assert captured["actor_id"] is None


def test_queue_export_reuses_queued_export_without_task_id_with_correlated_enqueue(
    db_session, subscriber_account, monkeypatch
):
    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.queued,
        requested_by_id=subscriber_account.id,
    )
    db_session.add(export)
    db_session.commit()
    db_session.refresh(export)

    captured: dict[str, object] = {}

    def _fake_enqueue(
        task,
        *,
        args=None,
        kwargs=None,
        correlation_id=None,
        source=None,
        actor_id=None,
        **extra,
    ):
        captured["task"] = task
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["correlation_id"] = correlation_id
        captured["source"] = source
        captured["actor_id"] = actor_id
        captured["extra"] = extra
        return type("AsyncResult", (), {"id": "task-queued-1"})()

    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    reused = pdf_service.queue_export(
        db_session,
        invoice_id=str(invoice.id),
        requested_by_id=str(subscriber_account.id),
    )

    assert reused.id == export.id
    assert reused.celery_task_id == "task-queued-1"
    assert captured["args"] == [str(export.id)]
    assert captured["kwargs"] is None
    assert captured["correlation_id"] == f"invoice_pdf_export:{export.id}"
    assert captured["source"] == "billing_invoice_pdf"
    assert captured["actor_id"] == str(subscriber_account.id)


def test_render_invoice_html_includes_branding_and_company_info(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account)
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.comms,
                key="sidebar_logo_url",
                value_text="data:image/png;base64,ZmFrZS1sb2dv",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_name",
                value_text="Dotmac Green ISP",
                value_type=SettingValueType.string,
            ),
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_email",
                value_text="billing@dotmac.ng",
                value_type=SettingValueType.string,
            ),
        ]
    )
    db_session.commit()

    html = pdf_service._render_invoice_html(invoice, db_session)

    assert "Dotmac Green ISP" in html
    assert "data:image/png;base64,ZmFrZS1sb2dv" in html
    assert "--green-900" in html
    assert "--red-700" in html


def test_completed_export_before_template_refresh_is_stale(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account)
    export = InvoicePdfExport(
        invoice_id=invoice.id,
        status=InvoicePdfExportStatus.completed,
        requested_by_id=subscriber_account.id,
        completed_at=pdf_service.INVOICE_PDF_TEMPLATE_REFRESHED_AT
        - timedelta(minutes=1),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert pdf_service._is_export_fresh(invoice, export) is False


def test_generate_export_now_uses_current_renderer(
    db_session, subscriber_account, monkeypatch
):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)
    SessionLocal = sessionmaker(
        bind=db_session.get_bind(), autoflush=False, autocommit=False
    )
    monkeypatch.setattr(pdf_service, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        pdf_service,
        "_build_pdf_bytes",
        lambda _db, _invoice: b"%PDF-1.4 branded-current",
    )

    invoice = _invoice(db_session, subscriber_account)
    export = pdf_service.generate_export_now(
        db_session,
        invoice_id=str(invoice.id),
        requested_by_id=str(subscriber_account.id),
    )

    assert export.status == InvoicePdfExportStatus.completed
    assert export.file_path in fake_storage.objects
    assert fake_storage.objects[export.file_path] == b"%PDF-1.4 branded-current"


def test_build_pdf_bytes_with_weasyprint_pydyf_compat(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account)

    pdf_bytes = pdf_service._build_pdf_bytes(db_session, invoice)

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1500
