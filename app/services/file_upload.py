"""
Unified File Upload Service.

Central service for file validation, storage, and deletion.
Upload services (avatar, branding, subscriber docs, attachments)
delegate to this core service.

Key features:
- Size validated BEFORE writing to disk
- Consistent path-traversal protection
- Magic byte validation (opt-in)
- SHA-256 checksum (opt-in)
- Shared helpers: coerce_uuid, format_file_size, compute_checksum_*
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Magic bytes for file-type validation (extension → list of valid signatures)
# ---------------------------------------------------------------------------
MAGIC_BYTES: dict[str, list[bytes]] = {
    # Documents
    ".pdf": [b"%PDF"],
    ".doc": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
    ".docx": [b"PK\x03\x04"],
    ".xls": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
    ".xlsx": [b"PK\x03\x04"],
    # Images
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".webp": [b"RIFF"],
    # Archives
    ".zip": [b"PK\x03\x04"],
}

# Content-type → extension mapping (superset across all services)
CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/zip": ".zip",
}

# Safe entity-type pattern (used by finance/PM attachment paths)
SAFE_ENTITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Shared helpers (used by multiple domain-specific attachment services)
# ---------------------------------------------------------------------------


def coerce_uuid(value: Union[str, uuid.UUID]) -> uuid.UUID:
    """Convert string to UUID if needed."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def format_file_size(size: int) -> str:
    """Format file size for human-readable display."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


def compute_checksum(data: bytes) -> str:
    """Compute SHA-256 checksum of in-memory bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_checksum_from_file(file_path: str) -> str:
    """Compute SHA-256 checksum of a file on disk (chunked)."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def resolve_safe_path(base_dir: Path, relative_path: str) -> Path:
    """
    Resolve a relative path safely within a base directory.

    Raises ValueError if the resolved path would escape base_dir.
    """
    base = base_dir.resolve()
    full_path = (base / relative_path).resolve()
    if base != full_path and base not in full_path.parents:
        raise ValueError("Invalid file path: outside upload directory")
    return full_path


def safe_entity_segment(entity_type: str) -> str:
    """Validate an entity-type string for use in filesystem paths."""
    if not entity_type:
        raise ValueError("Entity type is required")
    if not SAFE_ENTITY_PATTERN.match(entity_type):
        raise ValueError("Invalid entity type")
    if Path(entity_type).name != entity_type:
        raise ValueError("Invalid entity type")
    return entity_type


@dataclass(frozen=True)
class FileUploadConfig:
    """Policy object defining upload constraints for a specific domain."""

    base_dir: str
    allowed_content_types: frozenset[str]
    max_size_bytes: int
    allowed_extensions: frozenset[str] = field(default_factory=frozenset)
    require_magic_bytes: bool = False
    compute_checksum: bool = False


@dataclass
class UploadResult:
    """Result of a successful file upload."""

    file_path: Path
    relative_path: str
    filename: str
    file_size: int
    checksum: str | None = None


class FileUploadError(Exception):
    """Base exception for upload errors."""

    pass


class InvalidContentTypeError(FileUploadError):
    """Content type is not allowed."""

    pass


class InvalidExtensionError(FileUploadError):
    """File extension is not allowed."""

    pass


class FileTooLargeError(FileUploadError):
    """File exceeds size limit."""

    pass


class InvalidMagicBytesError(FileUploadError):
    """File content does not match its claimed format."""

    pass


class PathTraversalError(FileUploadError):
    """Attempted path traversal detected."""

    pass


class FileUploadService:
    """
    Core file upload service.

    Handles validation, storage, and deletion with consistent
    security guarantees across all upload domains.
    """

    def __init__(self, config: FileUploadConfig) -> None:
        self.config = config

    @property
    def base_path(self) -> Path:
        """Resolved base directory for uploads."""
        return Path(self.config.base_dir).resolve()

    def validate(
        self,
        content_type: str | None,
        filename: str | None,
        file_size: int,
        file_data: bytes | None = None,
    ) -> None:
        """
        Run all validation checks BEFORE writing to disk.

        Raises FileUploadError subclass on failure.
        """
        # Content type check
        if content_type and self.config.allowed_content_types:
            if content_type not in self.config.allowed_content_types:
                allowed = ", ".join(sorted(self.config.allowed_content_types))
                raise InvalidContentTypeError(
                    f"Content type '{content_type}' not allowed. Allowed: {allowed}"
                )

        # Extension check
        if filename and self.config.allowed_extensions:
            ext = Path(filename).suffix.lower()
            if ext not in self.config.allowed_extensions:
                allowed = ", ".join(sorted(self.config.allowed_extensions))
                raise InvalidExtensionError(
                    f"File extension '{ext}' not allowed. Allowed: {allowed}"
                )

        # Size check
        if file_size > self.config.max_size_bytes:
            max_mb = self.config.max_size_bytes / (1024 * 1024)
            raise FileTooLargeError(
                f"File too large ({file_size} bytes). Maximum size: {max_mb:.0f}MB"
            )

        # Magic bytes check
        if self.config.require_magic_bytes and file_data and filename:
            ext = Path(filename).suffix.lower()
            if ext in MAGIC_BYTES:
                valid = any(
                    file_data[: len(magic)] == magic for magic in MAGIC_BYTES[ext]
                )
                if not valid:
                    raise InvalidMagicBytesError(
                        "File content does not match the expected format"
                    )
            elif content_type in {"text/plain", "text/csv"}:
                try:
                    file_data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InvalidMagicBytesError(
                        "Text file contains invalid UTF-8 content"
                    ) from exc

    def _generate_filename(
        self,
        content_type: str | None,
        original_filename: str | None,
        prefix: str | None = None,
    ) -> str:
        """Generate a unique filename preserving the original extension."""
        ext = ""
        if original_filename:
            ext = Path(original_filename).suffix.lower()
        if not ext and content_type:
            ext = CONTENT_TYPE_EXTENSIONS.get(content_type, "")

        unique_id = uuid.uuid4().hex[:12]
        if prefix:
            return f"{prefix}_{unique_id}{ext}"
        return f"{unique_id}{ext}"

    def save(
        self,
        file_data: bytes,
        content_type: str | None = None,
        subdirs: Sequence[str] | None = None,
        prefix: str | None = None,
        original_filename: str | None = None,
    ) -> UploadResult:
        """
        Validate and save a file.

        Args:
            file_data: Raw file bytes.
            content_type: MIME type.
            subdirs: Optional subdirectory components (e.g. [org_id]).
            prefix: Optional filename prefix (e.g. "logo", "favicon").
            original_filename: Original filename for extension preservation.

        Returns:
            UploadResult with paths and metadata.

        Raises:
            FileUploadError subclass on validation failure.
        """
        # Validate BEFORE writing
        self.validate(content_type, original_filename, len(file_data), file_data)

        # Build target directory
        target_dir = self.base_path
        if subdirs:
            for sub in subdirs:
                target_dir = target_dir / sub
        target_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        filename = self._generate_filename(content_type, original_filename, prefix)
        file_path = target_dir / filename

        # Path traversal protection
        resolved = file_path.resolve()
        try:
            resolved.relative_to(self.base_path)
        except ValueError:
            raise PathTraversalError(
                "Path traversal detected: target is outside upload directory"
            )

        # Write
        resolved.write_bytes(file_data)

        # Relative path from base_dir
        relative = str(resolved.relative_to(self.base_path))

        # Optional checksum
        checksum: str | None = None
        if self.config.compute_checksum:
            checksum = hashlib.sha256(file_data).hexdigest()

        logger.info(
            "File saved: %s (%d bytes, type=%s)",
            relative,
            len(file_data),
            content_type,
        )

        return UploadResult(
            file_path=resolved,
            relative_path=relative,
            filename=filename,
            file_size=len(file_data),
            checksum=checksum,
        )

    def delete(self, relative_path: str) -> bool:
        """
        Delete a file by its relative path within base_dir.

        Returns True if deleted, False if not found.
        Raises PathTraversalError if path escapes base_dir.
        """
        target = (self.base_path / relative_path).resolve()
        try:
            target.relative_to(self.base_path)
        except ValueError:
            raise PathTraversalError(
                "Path traversal detected: target is outside upload directory"
            )

        if target.exists():
            target.unlink()
            logger.info("File deleted: %s", relative_path)
            return True
        return False

    def delete_by_url(self, url: str, url_prefix: str) -> bool:
        """
        Delete a file identified by its URL.

        Strips url_prefix to derive the relative path, then delegates
        to delete(). Used by avatar and branding services.
        """
        if not url or not url.startswith(url_prefix):
            return False

        relative = url[len(url_prefix) :].lstrip("/")
        if not relative:
            return False

        return self.delete(relative)


# ---------------------------------------------------------------------------
# Pre-configured instances for each upload domain
# ---------------------------------------------------------------------------


def _avatar_config() -> FileUploadConfig:
    """Avatar upload configuration."""
    import os

    return FileUploadConfig(
        base_dir=os.getenv("AVATAR_UPLOAD_DIR", "static/avatars"),
        allowed_content_types=frozenset(
            os.getenv(
                "AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/gif,image/webp"
            ).split(",")
        ),
        max_size_bytes=int(os.getenv("AVATAR_MAX_SIZE_BYTES", str(2 * 1024 * 1024))),
    )


def _branding_config() -> FileUploadConfig:
    """Branding (logo, favicon) upload configuration."""
    import os

    return FileUploadConfig(
        base_dir=os.getenv("BRANDING_UPLOAD_DIR", "static/branding"),
        allowed_content_types=frozenset(
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
        max_size_bytes=int(os.getenv("BRANDING_MAX_SIZE_BYTES", str(5 * 1024 * 1024))),
    )


def _subscriber_document_config() -> FileUploadConfig:
    """Subscriber document upload configuration (ID docs, contracts, etc.)."""
    import os

    return FileUploadConfig(
        base_dir=os.getenv("SUBSCRIBER_DOCS_DIR", "uploads/subscriber_docs"),
        allowed_content_types=frozenset(
            {
                "application/pdf",
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ),
        allowed_extensions=frozenset(
            {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".doc", ".docx"}
        ),
        max_size_bytes=int(
            os.getenv("SUBSCRIBER_DOC_MAX_SIZE", str(10 * 1024 * 1024))
        ),
        require_magic_bytes=True,
        compute_checksum=True,
    )


def _import_file_config() -> FileUploadConfig:
    """Bulk import file configuration (CSV, Excel)."""
    import os

    return FileUploadConfig(
        base_dir=os.getenv("IMPORT_UPLOAD_DIR", "uploads/imports"),
        allowed_content_types=frozenset(
            {
                "text/csv",
                "text/plain",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ),
        allowed_extensions=frozenset({".csv", ".xls", ".xlsx"}),
        max_size_bytes=int(os.getenv("IMPORT_MAX_SIZE", str(50 * 1024 * 1024))),
        compute_checksum=True,
    )


def _attachment_config() -> FileUploadConfig:
    """General attachment configuration (invoices, tickets, etc.)."""
    import os

    return FileUploadConfig(
        base_dir=os.getenv("ATTACHMENT_UPLOAD_DIR", "uploads/attachments"),
        allowed_content_types=frozenset(
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
        max_size_bytes=int(os.getenv("ATTACHMENT_MAX_SIZE", str(10 * 1024 * 1024))),
        compute_checksum=True,
    )


def get_avatar_upload() -> FileUploadService:
    """Get avatar upload service."""
    return FileUploadService(_avatar_config())


def get_branding_upload() -> FileUploadService:
    """Get branding upload service."""
    return FileUploadService(_branding_config())


def get_subscriber_document_upload() -> FileUploadService:
    """Get subscriber document upload service."""
    return FileUploadService(_subscriber_document_config())


def get_import_upload() -> FileUploadService:
    """Get bulk import file upload service."""
    return FileUploadService(_import_file_config())


def get_attachment_upload() -> FileUploadService:
    """Get general attachment upload service."""
    return FileUploadService(_attachment_config())
