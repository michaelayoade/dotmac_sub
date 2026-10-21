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
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.file_upload import MAGIC_BYTES
from app.services.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    S3StorageService,
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


@dataclass(frozen=True)
class PreparedFileUpload:
    """An object uploaded before its caller stages database metadata."""

    owner_subscriber_id: uuid.UUID | None
    entity_type: str
    entity_id: str
    original_filename: str
    storage_key: str
    file_size: int
    content_type: str
    checksum: str | None
    uploaded_by: str | None


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
    "catalogues": FileDomainConfig(
        prefix="catalogues",
        max_size_bytes=20 * 1024 * 1024,
        allowed_mime_types=frozenset({"application/pdf"}),
        allowed_extensions=frozenset({".pdf"}),
        require_magic_bytes=True,
        compute_checksum=True,
    ),
}


@dataclass(frozen=True, slots=True)
class FileRetentionQuery:
    file_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FileEvidenceLock:
    file_ids: tuple[uuid.UUID, ...]


def _lock_object_keys(db: Session, *, keys: tuple[tuple[str, str], ...]) -> None:
    if db.get_bind().dialect.name == "postgresql":
        for provider, storage_key in keys:
            lock_id = int.from_bytes(
                hashlib.sha256(
                    ("file-evidence:" + provider + ":" + storage_key).encode()
                ).digest()[:8],
                "big",
                signed=True,
            )
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})


def lock_files_for_evidence(
    db: Session, *, query: FileEvidenceLock
) -> tuple[StoredFile, ...]:
    """One common physical-object lock for reference creation and deletion."""
    from app.services.domain_errors import DomainError

    requested = db.scalars(
        select(StoredFile).where(StoredFile.id.in_(query.file_ids))
    ).all()
    if {file.id for file in requested} != set(query.file_ids):
        raise DomainError(
            code="files.not_found", message="File not found", retryable=False
        )
    keys = sorted(
        {
            (file.storage_provider, file.storage_key_or_relative_path)
            for file in requested
        }
    )
    _lock_object_keys(db, keys=tuple(keys))
    aliases: list[StoredFile] = []
    for provider, storage_key in keys:
        aliases.extend(
            db.scalars(
                select(StoredFile)
                .where(
                    StoredFile.storage_provider == provider,
                    StoredFile.storage_key_or_relative_path == storage_key,
                )
                .order_by(StoredFile.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
    if not set(query.file_ids).issubset({file.id for file in aliases}):
        raise DomainError(
            code="files.not_found",
            message="File reference changed while locking",
            retryable=False,
        )
    return tuple(aliases)


def lock_file_for_retention(db: Session, *, query: FileRetentionQuery) -> StoredFile:
    """Refuse retained physical objects before S3 effects or metadata mutation."""
    from app.models.stored_file_retention import StoredFileRetention
    from app.services.domain_errors import DomainError

    files = lock_files_for_evidence(db, query=FileEvidenceLock((query.file_id,)))
    if (
        db.scalar(
            select(StoredFileRetention.stored_file_id)
            .where(
                StoredFileRetention.stored_file_id.in_(tuple(file.id for file in files))
            )
            .limit(1)
        )
        is not None
    ):
        raise DomainError(
            code="files.evidence_retained",
            message="Completion evidence must be retained",
            retryable=False,
        )
    return next(file for file in files if file.id == query.file_id)


@dataclass(frozen=True, slots=True)
class CompletionFileRetentionRegistration:
    stored_file_id: uuid.UUID
    submission_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FileRetentionRegistered:
    stored_file_id: uuid.UUID
    reference_id: uuid.UUID


def stage_completion_retention(
    db: Session, *, command: CompletionFileRetentionRegistration
) -> FileRetentionRegistered:
    """Storage owns retention facts; completion owns the originating evidence."""
    from app.models.stored_file_retention import StoredFileRetention
    from app.services.domain_errors import DomainError
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.field_completion"):
        raise DomainError(
            code="files.retention_participant_required",
            message="Completion owner transaction required",
            retryable=False,
        )
    files = lock_files_for_evidence(
        db, query=FileEvidenceLock((command.stored_file_id,))
    )
    file = next(value for value in files if value.id == command.stored_file_id)
    if file.is_deleted:
        raise DomainError(
            code="files.not_found", message="File not found", retryable=False
        )
    if (
        file.entity_type != "field_attachment"
        or not file.checksum
        or file.file_size < 1
        or file.file_size > DOMAIN_CONFIGS["attachments"].max_size_bytes
    ):
        raise DomainError(
            code="files.evidence_unavailable",
            message="Completion evidence is unavailable",
            retryable=False,
        )
    # Verify actual retained bytes; metadata alone cannot prove an object survived
    # an earlier external-delete/database-rollback failure.
    digest = hashlib.sha256()
    size = 0
    try:
        stream = file_uploads.stream_file(file)
        for chunk in stream.chunks:
            size += len(chunk)
            if size > file.file_size:
                raise DomainError(
                    code="files.evidence_unavailable",
                    message="Completion evidence size does not match",
                    retryable=False,
                )
            digest.update(chunk)
    except ObjectNotFoundError as exc:
        raise DomainError(
            code="files.evidence_unavailable",
            message="Completion evidence object is missing",
            retryable=False,
        ) from exc
    except (ObjectStorageError, OSError) as exc:
        raise DomainError(
            code="files.storage_unavailable",
            message="Completion evidence could not be verified",
            retryable=True,
        ) from exc
    if size != file.file_size or digest.hexdigest() != file.checksum:
        raise DomainError(
            code="files.evidence_unavailable",
            message="Completion evidence checksum does not match",
            retryable=False,
        )
    key = (command.stored_file_id, command.submission_id)
    if db.get(StoredFileRetention, key) is None:
        db.add(
            StoredFileRetention(
                stored_file_id=command.stored_file_id,
                reference_id=command.submission_id,
                source_owner="operations.field_completion",
            )
        )
        db.flush()
    return FileRetentionRegistered(command.stored_file_id, command.submission_id)


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


def _tenant_segment(owner_subscriber_id: uuid.UUID | None) -> str:
    if owner_subscriber_id is None:
        return "public"
    return f"acct-{owner_subscriber_id}"


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


def _build_content_disposition(
    filename: str,
    *,
    disposition: Literal["attachment", "inline"],
) -> str:
    safe = _sanitize_filename(filename)
    quoted = safe.replace('"', "")
    return f'{disposition}; filename="{quoted}"'


def build_content_disposition(filename: str) -> str:
    return _build_content_disposition(filename, disposition="attachment")


def build_inline_content_disposition(filename: str) -> str:
    return _build_content_disposition(filename, disposition="inline")


class UnifiedFileUploadService:
    """Tenant-aware private file upload service."""

    def __init__(self) -> None:
        self.storage: S3StorageService | None = None

    def _storage_client(self):
        if self.storage is None:
            self.storage = get_s3_storage()
        return self.storage

    def get_domain_config(self, domain: str) -> FileDomainConfig:
        try:
            return DOMAIN_CONFIGS[domain]
        except KeyError as exc:
            raise FileValidationError(f"Unknown upload domain: {domain}") from exc

    def resolve_user_owner_subscriber(
        self, db: Session, subscriber_id: str
    ) -> uuid.UUID | None:
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return None
        if subscriber.category == SubscriberCategory.business:
            return subscriber.id
        return None

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
        owner_subscriber_id: uuid.UUID | None,
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
            f"{prefix}/{_tenant_segment(owner_subscriber_id)}/"
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
        owner_subscriber_id: uuid.UUID | None = None,
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
            owner_subscriber_id=owner_subscriber_id,
            entity_type=entity_type,
            entity_id=entity_id,
            file_bytes=data,
            extension=ext,
        )

        _lock_object_keys(db, keys=(("s3", storage_key),))
        self._storage_client().upload(storage_key, data, final_content_type)
        checksum = hashlib.sha256(data).hexdigest() if config.compute_checksum else None

        record = StoredFile(
            owner_subscriber_id=owner_subscriber_id,
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

    def stage_upload(
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
        owner_subscriber_id: uuid.UUID | None = None,
    ) -> StoredFile:
        """Stage file metadata in the caller's transaction.

        Object keys are content-addressed, so retrying the complete owner command
        is safe. The caller owns commit/rollback; orphan-object cleanup remains a
        storage reconciler concern and this participant never completes the
        database transaction.
        """
        prepared = self.prepare_upload(
            domain=domain,
            entity_type=entity_type,
            entity_id=entity_id,
            original_filename=original_filename,
            content_type=content_type,
            data=data,
            uploaded_by=uploaded_by,
            owner_subscriber_id=owner_subscriber_id,
        )
        return self.stage_prepared_upload(db=db, prepared=prepared)

    def prepare_upload(
        self,
        *,
        domain: str,
        entity_type: str,
        entity_id: str,
        original_filename: str,
        content_type: str | None,
        data: bytes,
        uploaded_by: str | None,
        owner_subscriber_id: uuid.UUID | None = None,
    ) -> PreparedFileUpload:
        """Validate and upload an object before any database lock is acquired."""
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
            owner_subscriber_id=owner_subscriber_id,
            entity_type=entity_type,
            entity_id=entity_id,
            file_bytes=data,
            extension=ext,
        )
        self._storage_client().upload(storage_key, data, final_content_type)
        checksum = hashlib.sha256(data).hexdigest() if config.compute_checksum else None
        return PreparedFileUpload(
            owner_subscriber_id=owner_subscriber_id,
            entity_type=entity_type,
            entity_id=entity_id,
            original_filename=safe_name,
            storage_key=storage_key,
            file_size=len(data),
            content_type=final_content_type,
            checksum=checksum,
            uploaded_by=uploaded_by,
        )

    def stage_prepared_upload(
        self, *, db: Session, prepared: PreparedFileUpload
    ) -> StoredFile:
        """Lock and verify a prepared object before creating active metadata."""
        from app.services.domain_errors import DomainError

        _lock_object_keys(db, keys=(("s3", prepared.storage_key),))
        try:
            exists = self._storage_client().exists(prepared.storage_key)
        except ObjectStorageError as exc:
            raise DomainError(
                code="files.storage_unavailable",
                message="Prepared object could not be verified",
                retryable=True,
            ) from exc
        if not exists:
            raise DomainError(
                code="files.prepared_object_missing",
                message="Prepared object is missing; prepare the upload again",
                retryable=True,
            )
        record = StoredFile(
            owner_subscriber_id=prepared.owner_subscriber_id,
            entity_type=prepared.entity_type,
            entity_id=prepared.entity_id,
            original_filename=prepared.original_filename,
            storage_key_or_relative_path=prepared.storage_key,
            file_size=prepared.file_size,
            content_type=prepared.content_type,
            checksum=prepared.checksum,
            storage_provider="s3",
            uploaded_by=(
                uuid.UUID(prepared.uploaded_by) if prepared.uploaded_by else None
            ),
            uploaded_at=datetime.now(UTC),
        )
        db.add(record)
        db.flush()
        logger.info(
            "file_upload_staged entity=%s entity_id=%s file_id=%s key=%s",
            prepared.entity_type,
            prepared.entity_id,
            record.id,
            prepared.storage_key,
        )
        return record

    def get_active_entity_file(
        self, db: Session, entity_type: str, entity_id: str
    ) -> StoredFile | None:
        return db.scalars(
            select(StoredFile)
            .where(StoredFile.entity_type == entity_type)
            .where(StoredFile.entity_id == entity_id)
            .where(StoredFile.is_deleted.is_(False))
            .order_by(StoredFile.created_at.desc())
        ).first()

    def assert_owner_access(
        self, file: StoredFile, current_owner_subscriber_id: uuid.UUID | None
    ) -> None:
        if file.entity_type in {"field_identity_portrait", "field_identity_evidence"}:
            raise HTTPException(status_code=404, detail="File not found")
        if file.owner_subscriber_id is None:
            return
        if current_owner_subscriber_id != file.owner_subscriber_id:
            logger.warning(
                "file_access_denied file_id=%s owner=%s request_owner=%s",
                file.id,
                file.owner_subscriber_id,
                current_owner_subscriber_id,
            )
            raise HTTPException(status_code=404, detail="File not found")

    def stream_file(self, file: StoredFile) -> StreamResult:
        if file.storage_provider == "s3":
            return self._storage_client().stream(file.storage_key_or_relative_path)
        if file.legacy_local_path:
            path = Path(file.legacy_local_path).resolve()
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
        file = lock_file_for_retention(db, query=FileRetentionQuery(file.id))
        aliases = lock_files_for_evidence(db, query=FileEvidenceLock((file.id,)))
        object_unreferenced = all(
            alias.id == file.id or alias.is_deleted for alias in aliases
        )
        if hard_delete_object and object_unreferenced and file.storage_provider == "s3":
            self._storage_client().delete(file.storage_key_or_relative_path)
        file.is_deleted = True
        file.deleted_at = datetime.now(UTC)
        db.add(file)
        db.commit()
        db.refresh(file)
        logger.info(
            "file_deleted file_id=%s hard_object_delete=%s", file.id, hard_delete_object
        )
        return file

    def stage_soft_delete(self, *, db: Session, file: StoredFile) -> StoredFile:
        """Deactivate file metadata without completing the caller's transaction.

        Physical deletion is deliberately excluded: deleting an object before a
        database commit would make rollback unable to restore the authoritative
        attachment. Retention cleanup can safely remove unreferenced objects later.
        """
        file = lock_file_for_retention(db, query=FileRetentionQuery(file.id))
        file.is_deleted = True
        file.deleted_at = datetime.now(UTC)
        db.add(file)
        db.flush()
        logger.info("file_delete_staged file_id=%s", file.id)
        return file


file_uploads = UnifiedFileUploadService()


@dataclass(frozen=True, slots=True)
class RetainedEvidenceStreamQuery:
    stored_file_id: uuid.UUID
    submission_id: uuid.UUID
    expected_checksum: str


@dataclass(frozen=True, slots=True)
class RetainedEvidenceStream:
    filename: str
    content_type: str
    file_size: int
    checksum: str
    stream: StreamResult


def stream_retained_evidence(
    db: Session, *, query: RetainedEvidenceStreamQuery
) -> RetainedEvidenceStream:
    """Internal storage participant; caller owns purpose-scoped native admission.

    Buffer at most the canonical attachment limit, verifying retained bytes before
    returning any content. No URL, storage key or general private-file grant escapes.
    """
    from app.models.stored_file_retention import StoredFileRetention
    from app.services.domain_errors import DomainError

    files = lock_files_for_evidence(db, query=FileEvidenceLock((query.stored_file_id,)))
    file = next(value for value in files if value.id == query.stored_file_id)
    retention = db.get(StoredFileRetention, (query.stored_file_id, query.submission_id))
    if (
        retention is None
        or retention.source_owner != "operations.field_completion"
        or file.is_deleted
        or file.entity_type != "field_attachment"
        or file.checksum != query.expected_checksum
        or file.file_size < 1
        or file.file_size > DOMAIN_CONFIGS["attachments"].max_size_bytes
    ):
        raise DomainError(
            code="files.evidence_unavailable",
            message="Retained evidence is unavailable",
            retryable=False,
        )
    data = bytearray()
    digest = hashlib.sha256()
    try:
        source = file_uploads.stream_file(file)
        for chunk in source.chunks:
            if len(data) + len(chunk) > file.file_size:
                raise DomainError(
                    code="files.evidence_unavailable",
                    message="Retained evidence size does not match",
                    retryable=False,
                )
            data.extend(chunk)
            digest.update(chunk)
    except ObjectNotFoundError as exc:
        raise DomainError(
            code="files.evidence_unavailable",
            message="Retained evidence object is missing",
            retryable=False,
        ) from exc
    except (ObjectStorageError, OSError) as exc:
        raise DomainError(
            code="files.storage_unavailable",
            message="Retained evidence could not be read",
            retryable=True,
        ) from exc
    if len(data) != file.file_size or digest.hexdigest() != query.expected_checksum:
        raise DomainError(
            code="files.evidence_unavailable",
            message="Retained evidence checksum does not match",
            retryable=False,
        )
    return RetainedEvidenceStream(
        filename=file.original_filename,
        content_type=file.content_type or "application/octet-stream",
        file_size=file.file_size,
        checksum=query.expected_checksum,
        stream=StreamResult(
            chunks=iter((bytes(data),)),
            content_type=file.content_type,
            content_length=file.file_size,
        ),
    )
