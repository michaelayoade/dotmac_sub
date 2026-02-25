"""Branding asset storage helpers for S3-backed rendering."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.stored_file import StoredFile
from app.services.file_storage import file_uploads

BRANDING_URL_PREFIX = "/branding/assets/"


def is_managed_branding_url(value: str) -> bool:
    return value.startswith(BRANDING_URL_PREFIX)


def branding_url_for_file(file_id: str | uuid.UUID) -> str:
    return f"{BRANDING_URL_PREFIX}{file_id}"


def file_id_from_branding_url(value: str) -> uuid.UUID | None:
    if not is_managed_branding_url(value):
        return None
    raw = value[len(BRANDING_URL_PREFIX) :].split("?", 1)[0].strip("/")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def upload_branding_asset(
    *,
    db: Session,
    setting_key: str,
    file_data: bytes,
    content_type: str | None,
    filename: str,
    uploaded_by: str | None,
) -> StoredFile:
    existing = file_uploads.get_active_entity_file(db, "branding_asset", setting_key)
    if existing:
        file_uploads.soft_delete(db=db, file=existing, hard_delete_object=True)
    return file_uploads.upload(
        db=db,
        domain="branding",
        entity_type="branding_asset",
        entity_id=setting_key,
        original_filename=filename,
        content_type=content_type,
        data=file_data,
        uploaded_by=uploaded_by,
        organization_id=None,
    )


def delete_managed_branding_url(db: Session, url: str) -> bool:
    file_id = file_id_from_branding_url(url)
    if not file_id:
        return False
    record = db.get(StoredFile, file_id)
    if not record or record.is_deleted:
        return False
    if record.entity_type != "branding_asset":
        return False
    file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
    return True
