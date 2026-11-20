from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

from app.models.stored_file import StoredFile
from app.services import web_support_tickets
from app.services.file_storage import PreparedFileUpload


def _stub_upload(monkeypatch, captured: dict[str, object], file_id):
    def fake_prepare(**kwargs):
        captured.update(kwargs)
        return PreparedFileUpload(
            owner_subscriber_id=kwargs["owner_subscriber_id"],
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key="attachments/public/test/file.pdf",
            file_size=len(kwargs["data"]),
            content_type=kwargs["content_type"],
            checksum=None,
            uploaded_by=kwargs["uploaded_by"],
        )

    def fake_stage(*, db, prepared):
        return StoredFile(
            id=file_id,
            original_filename=prepared.original_filename,
            content_type=prepared.content_type,
            file_size=prepared.file_size,
            storage_key_or_relative_path=prepared.storage_key,
            entity_type=prepared.entity_type,
            entity_id=prepared.entity_id,
        )

    monkeypatch.setattr(
        web_support_tickets.file_uploads, "prepare_upload", fake_prepare
    )
    monkeypatch.setattr(
        web_support_tickets.file_uploads, "stage_prepared_upload", fake_stage
    )


def test_support_ticket_attachments_use_subscriber_safe_uploaded_by(
    db_session, monkeypatch
):
    """Support ticket uploads keep file ownership out of subscriber scope."""
    captured: dict[str, object] = {}

    file_id = uuid4()

    _stub_upload(monkeypatch, captured, file_id)
    attachment = SimpleNamespace(
        filename="proof.pdf",
        content_type="application/pdf",
        file=BytesIO(b"%PDF-1.4 test"),
    )

    uploaded = web_support_tickets.upload_ticket_attachments.__wrapped__(
        db_session,
        ticket_id=uuid4(),
        attachments=[attachment],
        entity_type=web_support_tickets.TicketAttachmentEntityType.ticket,
        actor_id=None,
    )

    assert uploaded[0].stored_file_id == file_id
    assert captured["uploaded_by"] is None
    assert captured["owner_subscriber_id"] is None


def test_support_ticket_attachments_ignore_system_user_uploaded_by(
    db_session, monkeypatch
):
    captured: dict[str, object] = {}

    file_id = uuid4()

    _stub_upload(monkeypatch, captured, file_id)
    attachment = SimpleNamespace(
        filename="evidence.pdf",
        content_type="application/pdf",
        file=BytesIO(b"%PDF-1.4 test"),
    )

    uploaded = web_support_tickets.upload_ticket_attachments.__wrapped__(
        db_session,
        ticket_id=uuid4(),
        attachments=[attachment],
        entity_type=web_support_tickets.TicketAttachmentEntityType.comment,
        actor_id=uuid4(),
    )

    assert uploaded[0].stored_file_id == file_id
    assert captured["uploaded_by"] is None
