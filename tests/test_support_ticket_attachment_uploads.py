from io import BytesIO
from types import SimpleNamespace

from app.web.admin import support_tickets


def test_support_ticket_attachments_use_subscriber_safe_uploaded_by(
    db_session, monkeypatch
):
    """_upload_ticket_attachments uses _actor_id(request) for uploaded_by."""
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

    monkeypatch.setattr(support_tickets.file_uploads, "upload", _fake_upload)
    # _actor_id reads from get_current_user; patch it to return None (no subscriber_id)
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"subscriber_id": None},
    )

    request = SimpleNamespace(
        state=SimpleNamespace(
            user=SimpleNamespace(
                id="system-user-1",
                person_id=None,
                first_name="Admin",
                last_name="User",
                email="admin@example.com",
            ),
            auth={"principal_type": "system_user"},
        )
    )
    attachment = SimpleNamespace(
        filename="proof.pdf",
        content_type="application/pdf",
        file=BytesIO(b"%PDF-1.4 test"),
    )

    uploaded = support_tickets._upload_ticket_attachments(
        db_session,
        request=request,
        ticket_id="ticket-1",
        attachments=[attachment],
        entity_type="support_ticket_attachment",
    )

    assert uploaded[0]["stored_file_id"] == "file-1"
    assert captured["uploaded_by"] is None
    assert captured["owner_subscriber_id"] is None
