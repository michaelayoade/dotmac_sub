from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.stored_file import StoredFile
from app.models.subscriber import Organization
from app.services.file_storage import FileValidationError, file_uploads
from app.services.object_storage import StreamResult


def test_validate_rejects_invalid_extension():
    config = file_uploads.get_domain_config("legal_documents")
    with pytest.raises(FileValidationError):
        file_uploads.validate(
            config=config,
            filename="payload.exe",
            content_type="application/octet-stream",
            data=b"123",
        )


def test_generate_storage_key_contains_tenant_entity():
    org_id = uuid.uuid4()
    key = file_uploads.generate_storage_key(
        prefix="attachments",
        organization_id=org_id,
        entity_type="invoice",
        entity_id="abc-123",
        file_bytes=b"hello",
        extension=".pdf",
    )
    assert key.startswith(f"attachments/org-{org_id}/invoice/abc_123/")
    assert key.endswith(".pdf")


def test_upload_persists_metadata(db_session, monkeypatch):
    captured: dict[str, str] = {}

    class _Storage:
        def upload(self, key: str, data: bytes, content_type: str | None):
            captured["key"] = key
            captured["ct"] = content_type or ""

    monkeypatch.setattr(file_uploads, "storage", _Storage())

    org = Organization(name="Acme Fiber")
    db_session.add(org)
    db_session.commit()

    uploaded = file_uploads.upload(
        db=db_session,
        domain="legal_documents",
        entity_type="legal_document",
        entity_id=str(uuid.uuid4()),
        original_filename="terms.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 file",
        uploaded_by=None,
        organization_id=org.id,
    )
    assert uploaded.id is not None
    assert uploaded.storage_provider == "s3"
    assert uploaded.organization_id == org.id
    assert captured["key"] == uploaded.storage_key_or_relative_path


def test_soft_delete_marks_deleted(db_session, monkeypatch):
    calls: list[str] = []

    class _Storage:
        def delete(self, key: str):
            calls.append(key)

    monkeypatch.setattr(file_uploads, "storage", _Storage())

    record = StoredFile(
        organization_id=None,
        entity_type="test_entity",
        entity_id="123",
        original_filename="test.txt",
        storage_key_or_relative_path="prefix/public/test_entity/123/test.txt",
        file_size=4,
        content_type="text/plain",
        storage_provider="s3",
        uploaded_by=None,
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)

    deleted = file_uploads.soft_delete(db=db_session, file=record, hard_delete_object=True)
    assert deleted.is_deleted is True
    assert deleted.deleted_at is not None
    assert calls == [record.storage_key_or_relative_path]


def test_tenant_access_denied():
    record = StoredFile(
        organization_id=uuid.uuid4(),
        entity_type="test",
        entity_id="1",
        original_filename="f.txt",
        storage_key_or_relative_path="x",
        file_size=1,
        content_type="text/plain",
        storage_provider="s3",
        uploaded_by=None,
    )
    with pytest.raises(HTTPException) as exc:
        file_uploads.assert_tenant_access(record, uuid.uuid4())
    assert exc.value.status_code == 404


def test_stream_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.file_storage.settings",
        SimpleNamespace(base_upload_dir=str(tmp_path)),
    )
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(b"legacy")

    record = StoredFile(
        organization_id=None,
        entity_type="test",
        entity_id="1",
        original_filename="legacy.txt",
        storage_key_or_relative_path="legacy/path",
        legacy_local_path=str(legacy),
        file_size=6,
        content_type="text/plain",
        storage_provider="local",
        uploaded_by=None,
    )
    stream = file_uploads.stream_file(record)
    assert isinstance(stream, StreamResult)
    assert b"".join(stream.chunks) == b"legacy"


def test_stream_legacy_file_denies_path_outside_upload_root(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"blocked")
    monkeypatch.setattr(
        "app.services.file_storage.settings",
        SimpleNamespace(base_upload_dir=str(upload_root)),
    )

    record = StoredFile(
        organization_id=None,
        entity_type="test",
        entity_id="1",
        original_filename="outside.txt",
        storage_key_or_relative_path="legacy/path",
        legacy_local_path=str(outside),
        file_size=7,
        content_type="text/plain",
        storage_provider="local",
        uploaded_by=None,
    )

    with pytest.raises(
        PermissionError, match="Access denied: path outside upload directory"
    ):
        file_uploads.stream_file(record)
