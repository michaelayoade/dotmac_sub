"""Web helpers for admin legal document routes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.legal import LegalDocumentType
from app.schemas.legal import LegalDocumentCreate, LegalDocumentUpdate
from app.services import legal as legal_service

ALLOWED_UPLOAD_TYPES = {
    "application/pdf",
    "text/html",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024


def _base_context(request: Request, db: Session) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "legal",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def document_type_options() -> list[tuple[str, str]]:
    return [(t.value, t.value.replace("_", " ").title()) for t in LegalDocumentType]


def _parse_document_type(document_type: str | None) -> LegalDocumentType | None:
    if not document_type:
        return None
    try:
        return LegalDocumentType(document_type)
    except ValueError:
        return None


def _parse_published_filter(is_published: str | None) -> bool | None:
    if is_published == "true":
        return True
    if is_published == "false":
        return False
    return None


def _parse_effective_date(effective_date: str | None) -> datetime | None:
    if not effective_date:
        return None
    return datetime.fromisoformat(effective_date.replace("Z", "+00:00"))


def list_context(
    request: Request,
    db: Session,
    *,
    document_type: str | None = None,
    is_published: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    doc_type = _parse_document_type(document_type)
    published = _parse_published_filter(is_published)
    offset = (page - 1) * per_page

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

    context = _base_context(request, db)
    context.update(
        {
            "documents": documents,
            "stats": stats,
            "document_types": [t.value for t in LegalDocumentType],
            "document_type_filter": document_type,
            "is_published_filter": is_published,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
    )
    return context


def form_context(
    request: Request,
    db: Session,
    *,
    document=None,
    action: str = "create",
    error: str | None = None,
) -> dict[str, object]:
    context = _base_context(request, db)
    context.update(
        {
            "document": document,
            "document_types": document_type_options(),
            "action": action,
        }
    )
    if error:
        context["error"] = error
    return context


def detail_context(request: Request, db: Session, *, document) -> dict[str, object]:
    context = _base_context(request, db)
    context["document"] = document
    return context


def get_document(db: Session, document_id: str):
    return legal_service.legal_documents.get(db=db, document_id=document_id)


def create_document(
    db: Session,
    *,
    document_type: str,
    title: str,
    slug: str,
    version: str,
    summary: str | None,
    content: str | None,
    is_published: str | None,
    effective_date: str | None,
):
    payload = LegalDocumentCreate(
        document_type=LegalDocumentType(document_type),
        title=title,
        slug=slug,
        version=version,
        summary=summary or None,
        content=content or None,
        is_published=is_published == "true",
        effective_date=_parse_effective_date(effective_date),
    )
    return legal_service.legal_documents.create(db=db, payload=payload)


def update_document(
    db: Session,
    *,
    document_id: str,
    title: str,
    slug: str,
    version: str,
    summary: str | None,
    content: str | None,
    is_current: str | None,
    is_published: str | None,
    effective_date: str | None,
):
    payload = LegalDocumentUpdate(
        title=title,
        slug=slug,
        version=version,
        summary=summary or None,
        content=content or None,
        is_current=is_current == "true",
        is_published=is_published == "true",
        effective_date=_parse_effective_date(effective_date),
    )
    return legal_service.legal_documents.update(
        db=db, document_id=document_id, payload=payload
    )


def upload_document_file(
    request: Request,
    db: Session,
    *,
    document_id: str,
    file_content: bytes,
    file_name: str,
    mime_type: str | None,
):
    if mime_type not in ALLOWED_UPLOAD_TYPES:
        raise ValueError(
            "File type not allowed. Allowed types: PDF, HTML, TXT, DOC, DOCX"
        )
    if len(file_content) > MAX_UPLOAD_SIZE:
        raise ValueError("File size exceeds 10MB limit")

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return legal_service.legal_documents.upload_file(
        db=db,
        document_id=document_id,
        file_content=file_content,
        file_name=file_name,
        mime_type=mime_type,
        uploaded_by=current_user.get("subscriber_id") or None,
    )


def delete_document_file(db: Session, *, document_id: str):
    return legal_service.legal_documents.delete_file(db=db, document_id=document_id)


def publish_document(db: Session, *, document_id: str):
    payload = LegalDocumentUpdate.model_validate(
        {"is_published": True, "is_current": True}
    )
    return legal_service.legal_documents.update(
        db=db, document_id=document_id, payload=payload
    )


def unpublish_document(db: Session, *, document_id: str):
    payload = LegalDocumentUpdate.model_validate({"is_published": False})
    return legal_service.legal_documents.update(
        db=db, document_id=document_id, payload=payload
    )


def delete_document(db: Session, *, document_id: str) -> bool:
    return legal_service.legal_documents.delete(db=db, document_id=document_id)
