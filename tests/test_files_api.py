from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from app.api import files as files_api
from app.models.stored_file import StoredFile
from app.models.subscriber import Organization, Subscriber
from app.services.file_storage import file_uploads
from app.services.object_storage import ObjectNotFoundError, StreamResult


def _subscriber(email: str, org_id):
    return Subscriber(
        first_name="T",
        last_name="User",
        email=email,
        organization_id=org_id,
    )


def test_authenticated_download_and_tenant_isolation(db_session, monkeypatch):
    org_a = Organization(name="Org A")
    org_b = Organization(name="Org B")
    db_session.add_all([org_a, org_b])
    db_session.commit()

    user_a = _subscriber("a@example.com", org_a.id)
    user_b = _subscriber("b@example.com", org_b.id)
    db_session.add_all([user_a, user_b])
    db_session.commit()

    record = StoredFile(
        organization_id=org_a.id,
        entity_type="ticket",
        entity_id=str(uuid.uuid4()),
        original_filename="evidence.pdf",
        storage_key_or_relative_path="attachments/org-a/ticket/1/file.pdf",
        file_size=4,
        content_type="application/pdf",
        storage_provider="s3",
        uploaded_by=user_a.id,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)

    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda _record: StreamResult(iter([b"data"]), "application/pdf", 4),
    )

    response = files_api.download_file(
        str(record.id),
        db=db_session,
        current_user={"subscriber_id": str(user_a.id)},
    )
    assert isinstance(response, StreamingResponse)
    assert response.headers["content-disposition"].startswith("attachment;")

    with pytest.raises(HTTPException) as exc:
        files_api.download_file(
            str(record.id),
            db=db_session,
            current_user={"subscriber_id": str(user_b.id)},
        )
    assert exc.value.status_code == 404


def test_download_missing_object_and_deleted_record(db_session, monkeypatch):
    org = Organization(name="Org")
    db_session.add(org)
    db_session.commit()
    user = _subscriber("c@example.com", org.id)
    db_session.add(user)
    db_session.commit()

    active_record = StoredFile(
        organization_id=org.id,
        entity_type="ticket",
        entity_id=str(uuid.uuid4()),
        original_filename="proof.txt",
        storage_key_or_relative_path="attachments/key",
        file_size=5,
        content_type="text/plain",
        storage_provider="s3",
        uploaded_by=user.id,
    )
    deleted_record = StoredFile(
        organization_id=org.id,
        entity_type="ticket",
        entity_id=str(uuid.uuid4()),
        original_filename="gone.txt",
        storage_key_or_relative_path="attachments/key2",
        file_size=5,
        content_type="text/plain",
        storage_provider="s3",
        uploaded_by=user.id,
        is_deleted=True,
    )
    db_session.add_all([active_record, deleted_record])
    db_session.commit()

    def _raise(_record):
        raise ObjectNotFoundError("missing")

    monkeypatch.setattr(file_uploads, "stream_file", _raise)
    with pytest.raises(HTTPException) as exc:
        files_api.download_file(
            str(active_record.id),
            db=db_session,
            current_user={"subscriber_id": str(user.id)},
        )
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc2:
        files_api.download_file(
            str(deleted_record.id),
            db=db_session,
            current_user={"subscriber_id": str(user.id)},
        )
    assert exc2.value.status_code == 404
