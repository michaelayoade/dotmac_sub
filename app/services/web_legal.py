"""Service helpers for admin legal web routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import UploadFile

from app.models.legal import LegalDocumentType
from app.schemas.legal import LegalDocumentCreate, LegalDocumentUpdate
from app.services import legal as legal_service

ALLOWED_UPLOAD_CONTENT_TYPES = {
    "application/pdf",
    "text/html",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024


def parse_document_type_filter(document_type: str | None) -> LegalDocumentType | None:
    if not document_type:
        return None
    try:
        return LegalDocumentType(document_type)
    except ValueError:
        return None


def parse_published_filter(is_published: str | None) -> bool | None:
    if is_published == "true":
        return True
    if is_published == "false":
        return False
    return None


def parse_effective_date(effective_date: str | None) -> datetime | None:
    if not effective_date:
        return None
    return datetime.fromisoformat(effective_date.replace("Z", "+00:00"))


def list_page_data(
    db,
    *,
    document_type: str | None,
    is_published: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    doc_type = parse_document_type_filter(document_type)
    published = parse_published_filter(is_published)

    documents = legal_service.legal_documents.list(
        db=db,
        document_type=doc_type,
        is_published=published,
        order_by="updated_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    stats = legal_service.legal_documents.get_list_stats(
        db,
        document_type=doc_type,
        is_published=published,
    )
    total_pages = (stats["total"] + per_page - 1) // per_page

    return {
        "documents": documents,
        "stats": stats,
        "document_types": [t.value for t in LegalDocumentType],
        "document_type_filter": document_type,
        "is_published_filter": is_published,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


def document_type_options() -> list[tuple[str, str]]:
    return [(t.value, t.value.replace("_", " ").title()) for t in LegalDocumentType]


def build_document_create_payload(
    *,
    document_type: str,
    title: str,
    slug: str,
    version: str,
    summary: str | None,
    content: str | None,
    is_published: str | None,
    effective_date: str | None,
) -> LegalDocumentCreate:
    return LegalDocumentCreate(
        document_type=LegalDocumentType(document_type),
        title=title,
        slug=slug,
        version=version,
        summary=summary if summary else None,
        content=content if content else None,
        is_published=is_published == "true",
        effective_date=parse_effective_date(effective_date),
    )


def build_document_update_payload(
    *,
    title: str,
    slug: str,
    version: str,
    summary: str | None,
    content: str | None,
    is_current: str | None,
    is_published: str | None,
    effective_date: str | None,
) -> LegalDocumentUpdate:
    return LegalDocumentUpdate(
        title=title,
        slug=slug,
        version=version,
        summary=summary if summary else None,
        content=content if content else None,
        is_current=is_current == "true",
        is_published=is_published == "true",
        effective_date=parse_effective_date(effective_date),
    )


def read_and_validate_upload(file: UploadFile) -> tuple[bytes, str, str]:
    if file.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        raise ValueError("File type not allowed. Allowed types: PDF, HTML, TXT, DOC, DOCX")

    content = file.file.read()
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("File size exceeds 10MB limit")

    return content, (file.filename or "document"), file.content_type or "application/octet-stream"
