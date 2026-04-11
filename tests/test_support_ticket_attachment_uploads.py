from io import BytesIO
from types import SimpleNamespace

from app.services import web_support_tickets


def test_support_ticket_attachments_use_subscriber_safe_uploaded_by(
    db_session, monkeypatch
):
    """Support ticket uploads keep file ownership out of subscriber scope."""
    captured: dict[str, object] = {}

    def _fake_upload(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="file-1",
            original_filename=kwargs["original_filename"],
            content_type=kwargs["content_type"],
            file_size=len(kwargs["data"]),
            storage_key_or_relative_path="attachments/public/support_ticket/ticket-1/file.pdf",
        )

    monkeypatch.setattr(web_support_tickets.file_uploads, "upload", _fake_upload)
    attachment = SimpleNamespace(
        filename="proof.pdf",
        content_type="application/pdf",
        file=BytesIO(b"%PDF-1.4 test"),
    )

    uploaded = web_support_tickets.upload_ticket_attachments(
        db_session,
        ticket_id="ticket-1",
        attachments=[attachment],
        entity_type="support_ticket_attachment",
        actor_id=None,
    )

    assert uploaded[0]["stored_file_id"] == "file-1"
    assert captured["uploaded_by"] is None
    assert captured["owner_subscriber_id"] is None
