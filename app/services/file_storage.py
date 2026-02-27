"""Unified file upload + metadata service for private object storage."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber
from app.services.file_upload import MAGIC_BYTES
from app.services.object_storage import (
    ObjectNotFoundError,
    StreamResult,
    get_s3_storage,
)

logger = logging.getLogger(__name__)

SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class FileDomainConfig:
    prefix: str
    max_size_bytes: int
    allowed_mime_types: frozenset[str]
    allowed_extensions: frozenset[str]
    require_magic_bytes: bool = False
    compute_checksum: bool = False


DOMAIN_CONFIGS: dict[str, FileDomainConfig] = {
    "branding": FileDomainConfig(
        prefix="branding",
        max_size_bytes=5 * 1024 * 1024,
        allowed_mime_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "image/svg+xml",
                "image/x-icon",
                "image/vnd.microsoft.icon",
            }
        ),
        allowed_extensions=frozenset(
            {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"}
        ),
    ),
    "attachments": FileDomainConfig(
        prefix="attachments",
        max_size_bytes=10 * 1024 * 1024,
        allowed_mime_types=frozenset(
            {
                "application/pdf",
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/plain",
                "text/csv",
                "application/zip",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".pdf",
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".doc",
                ".docx",
                ".xls",
                ".xlsx",
                ".csv",
                ".txt",
                ".zip",
            }
        ),
        compute_checksum=True,
    ),
    "resumes": FileDomainConfig(
        prefix="resumes",
        max_size_bytes=5 * 1024 * 1024,
        allowed_mime_types=frozenset(
            {
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ),
        allowed_extensions=frozenset({".pdf", ".doc", ".docx"}),
        require_magic_bytes=True,
        compute_checksum=True,
    ),
    "avatars": FileDomainConfig(
        prefix="avatars",
        max_size_bytes=2 * 1024 * 1024,
        allowed_mime_types=frozenset(
            {"image/jpeg", "image/png", "image/gif", "image/webp"}
        ),
        allowed_extensions=frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"}),
        require_magic_bytes=True,
    ),
    "generated_docs": FileDomainConfig(
        prefix="generated_docs",
        max_size_bytes=20 * 1024 * 1024,
        allowed_mime_types=frozenset(
            {
                "application/pdf",
                "text/plain",
                "text/html",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ),
        allowed_extensions=frozenset({".pdf", ".txt", ".html", ".doc", ".docx"}),
        require_magic_bytes=True,
        compute_checksum=True,
    ),
    "legal_documents": FileDomainConfig(
        prefix="legal_documents",
        max_size_bytes=10 * 1024 * 1024,
        allowed_mime_types=frozenset(
            {
                "application/pdf",
                "text/plain",
                "text/html",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ),
        allowed_extensions=frozenset({".pdf", ".txt", ".html", ".doc", ".docx"}),
        require_magic_bytes=True,
        compute_checksum=True,
    ),
}


class FileValidationError(ValueError):
    """File validation failure."""


def _sanitize_filename(filename: str) -> str:
    name = Path(filename).name
    cleaned = SAFE_FILENAME_RE.sub("_", name).strip().strip(".")
    return cleaned[:255] or "file"


def _safe_segment(value: str) -> str:
    if not SAFE_SEGMENT_RE.match(value):
        raise FileValidationError("Unsafe path segment")
    return value


def _tenant_segment(organization_id: uuid.UUID | None) -> str:
    if organization_id is None:
        return "public"
    return f"org-{organization_id}"


def _magic_valid(data: bytes, ext: str, content_type: str | None) -> bool:
    if ext in MAGIC_BYTES:
        signatures = MAGIC_BYTES[ext]
        return any(data[: len(sig)] == sig for sig in signatures)
    if content_type in {"text/plain", "text/csv", "text/html"}:
        try:
            data.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return True


def build_content_disposition(filename: str) -> str:
    safe = _sanitize_filename(filename)
    quoted = safe.replace('"', "")
    return f'attachment; filename="{quoted}"'


class UnifiedFileUploadService:
    """Tenant-aware private file upload service."""

    def __init__(self) -> None:
        self.storage = None

    def _storage_client(self):
        if self.storage is None:
            self.storage = get_s3_storage()
        return self.storage

    def get_domain_config(self, domain: str) -> FileDomainConfig:
        try:
            return DOMAIN_CONFIGS[domain]
        except KeyError as exc:
            raise FileValidationError(f"Unknown upload domain: {domain}") from exc

    def resolve_user_organization(
        self, db: Session, subscriber_id: str
    ) -> uuid.UUID | None:
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return None
        return subscriber.organization_id

    def validate(
        self,
        *,
        config: FileDomainConfig,
        filename: str,
        content_type: str | None,
        data: bytes,
    ) -> tuple[str, str]:
        if len(data) > config.max_size_bytes:
            raise FileValidationError("File exceeds maximum allowed size")

        sanitized_filename = _sanitize_filename(filename)
        ext = Path(sanitized_filename).suffix.lower()
        if ext not in config.allowed_extensions:
            raise FileValidationError("File extension not allowed")

        guessed_type = mimetypes.guess_type(sanitized_filename)[0]
        if content_type and content_type not in config.allowed_mime_types:
            raise FileValidationError("MIME type not allowed")
        if guessed_type and guessed_type not in config.allowed_mime_types:
            raise FileValidationError("Filename extension resolves to disallowed MIME")

        if config.require_magic_bytes and not _magic_valid(data, ext, content_type):
            raise FileValidationError("File signature does not match expected format")

        final_type = content_type or guessed_type or "application/octet-stream"
        return sanitized_filename, final_type

    def generate_storage_key(
        self,
        *,
        prefix: str,
        organization_id: uuid.UUID | None,
        entity_type: str,
        entity_id: str,
        file_bytes: bytes,
        extension: str,
    ) -> str:
        entity_segment = _safe_segment(entity_type)
        entity_id_segment = _safe_segment(entity_id.replace("-", "_"))
        checksum = hashlib.sha256(file_bytes).hexdigest()
        generated_filename = f"{checksum[:24]}{extension.lower()}"
        return (
            f"{prefix}/{_tenant_segment(organization_id)}/"
            f"{entity_segment}/{entity_id_segment}/{generated_filename}"
        )

    def upload(
        self,
        *,
        db: Session,
        domain: str,
        entity_type: str,
        entity_id: str,
        original_filename: str,
        content_type: str | None,
        data: bytes,
        uploaded_by: str | None,
        organization_id: uuid.UUID | None = None,
    ) -> StoredFile:
        config = self.get_domain_config(domain)
        safe_name, final_content_type = self.validate(
            config=config,
            filename=original_filename,
            content_type=content_type,
            data=data,
        )
        ext = Path(safe_name).suffix.lower()
        storage_key = self.generate_storage_key(
            prefix=config.prefix,
            organization_id=organization_id,
            entity_type=entity_type,
            entity_id=entity_id,
            file_bytes=data,
            extension=ext,
        )

        self._storage_client().upload(storage_key, data, final_content_type)
        checksum = hashlib.sha256(data).hexdigest() if config.compute_checksum else None

        record = StoredFile(
            organization_id=organization_id,
            entity_type=entity_type,
            entity_id=entity_id,
            original_filename=safe_name,
            storage_key_or_relative_path=storage_key,
            file_size=len(data),
            content_type=final_content_type,
            checksum=checksum,
            storage_provider="s3",
            uploaded_by=uploaded_by,
            uploaded_at=datetime.now(UTC),
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        logger.info(
            "file_upload_success entity=%s entity_id=%s file_id=%s key=%s",
            entity_type,
            entity_id,
            record.id,
            storage_key,
        )
        return record

    def get_active_entity_file(
        self, db: Session, entity_type: str, entity_id: str
    ) -> StoredFile | None:
        return (
            db.query(StoredFile)
            .filter(StoredFile.entity_type == entity_type)
            .filter(StoredFile.entity_id == entity_id)
            .filter(StoredFile.is_deleted.is_(False))
            .order_by(StoredFile.created_at.desc())
            .first()
        )

    def assert_tenant_access(
        self, file: StoredFile, current_org_id: uuid.UUID | None
    ) -> None:
        if file.organization_id is None:
            return
        if current_org_id != file.organization_id:
            logger.warning(
                "file_access_denied file_id=%s org=%s request_org=%s",
                file.id,
                file.organization_id,
                current_org_id,
            )
            raise HTTPException(status_code=404, detail="File not found")

    def stream_file(self, file: StoredFile) -> StreamResult:
        if file.storage_provider == "s3":
            return self._storage_client().stream(file.storage_key_or_relative_path)
        if file.legacy_local_path:
            path = Path(file.legacy_local_path).resolve()
            base_upload_dir = Path(settings.base_upload_dir).resolve()
            try:
                path.relative_to(base_upload_dir)
            except ValueError as exc:
                raise PermissionError("Access denied: path outside upload directory") from exc
            if not path.exists():
                raise ObjectNotFoundError(str(path))

            def _chunks() -> Iterator[bytes]:
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk

            return StreamResult(
                chunks=_chunks(),
                content_type=file.content_type,
                content_length=file.file_size,
            )
        raise ObjectNotFoundError(str(file.id))

    def soft_delete(
        self,
        *,
        db: Session,
        file: StoredFile,
        hard_delete_object: bool = True,
    ) -> StoredFile:
        if hard_delete_object and file.storage_provider == "s3":
            self._storage_client().delete(file.storage_key_or_relative_path)
        file.is_deleted = True
        file.deleted_at = datetime.now(UTC)
        db.add(file)
        db.commit()
        db.refresh(file)
        logger.info("file_deleted file_id=%s hard_object_delete=%s", file.id, hard_delete_object)
        return file


file_uploads = UnifiedFileUploadService()
