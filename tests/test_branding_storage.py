from __future__ import annotations

import uuid

from app.models.stored_file import StoredFile
from app.services import branding_storage as branding_storage_service
from app.services.file_storage import file_uploads


class _FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload(self, key: str, data: bytes, content_type: str | None):
        self.objects[key] = data

    def delete(self, key: str):
        self.objects.pop(key, None)


def test_branding_url_helpers():
    file_id = uuid.uuid4()
    url = branding_storage_service.branding_url_for_file(file_id)
    assert branding_storage_service.is_managed_branding_url(url) is True
    assert branding_storage_service.file_id_from_branding_url(url) == file_id


def test_upload_branding_asset_and_delete(db_session, monkeypatch):
    fake_storage = _FakeStorage()
    monkeypatch.setattr(file_uploads, "storage", fake_storage)

    record = branding_storage_service.upload_branding_asset(
        db=db_session,
        setting_key="sidebar_logo_url",
        file_data=b"fake png",
        content_type="image/png",
        filename="logo.png",
        uploaded_by=None,
    )
    assert record.entity_type == "branding_asset"
    assert record.storage_key_or_relative_path in fake_storage.objects

    url = branding_storage_service.branding_url_for_file(record.id)
    assert branding_storage_service.delete_managed_branding_url(db_session, url) is True

    stored = db_session.get(StoredFile, record.id)
    assert stored is not None
    assert stored.is_deleted is True
