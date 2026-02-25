"""Service helpers for public legal document pages."""

from fastapi import Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models.legal import LegalDocumentType
from app.services import legal as legal_service
from app.services.file_storage import build_content_disposition
from app.services.object_storage import ObjectNotFoundError

templates = Jinja2Templates(directory="templates")


def _render_document(
    request: Request,
    document,
    document_type: str,
    fallback_title: str,
    fallback_content: str,
):
    return templates.TemplateResponse(
        "public/legal/document.html",
        {
            "request": request,
            "document": document,
            "document_type": document_type,
            "fallback_title": fallback_title,
            "fallback_content": fallback_content,
        },
    )


def privacy_policy(request: Request, db: Session):
    document = legal_service.legal_documents.get_current_by_type(
        db=db, document_type=LegalDocumentType.privacy_policy
    )
    return _render_document(
        request,
        document,
        "Privacy Policy",
        "Privacy Policy",
        "Privacy policy content is being prepared.",
    )


def terms_of_service(request: Request, db: Session):
    document = legal_service.legal_documents.get_current_by_type(
        db=db, document_type=LegalDocumentType.terms_of_service
    )
    return _render_document(
        request,
        document,
        "Terms of Service",
        "Terms of Service",
        "Terms of service content is being prepared.",
    )


def acceptable_use(request: Request, db: Session):
    document = legal_service.legal_documents.get_current_by_type(
        db=db, document_type=LegalDocumentType.acceptable_use
    )
    return _render_document(
        request,
        document,
        "Acceptable Use Policy",
        "Acceptable Use Policy",
        "Acceptable use policy content is being prepared.",
    )


def service_level_agreement(request: Request, db: Session):
    document = legal_service.legal_documents.get_current_by_type(
        db=db, document_type=LegalDocumentType.service_level_agreement
    )
    return _render_document(
        request,
        document,
        "Service Level Agreement",
        "Service Level Agreement",
        "Service level agreement content is being prepared.",
    )


def cookie_policy(request: Request, db: Session):
    document = legal_service.legal_documents.get_current_by_type(
        db=db, document_type=LegalDocumentType.cookie_policy
    )
    return _render_document(
        request,
        document,
        "Cookie Policy",
        "Cookie Policy",
        "Cookie policy content is being prepared.",
    )


def refund_policy(request: Request, db: Session):
    document = legal_service.legal_documents.get_current_by_type(
        db=db, document_type=LegalDocumentType.refund_policy
    )
    return _render_document(
        request,
        document,
        "Refund Policy",
        "Refund Policy",
        "Refund policy content is being prepared.",
    )


def legal_document_by_slug(request: Request, db: Session, slug: str):
    document = legal_service.legal_documents.get_by_slug(db=db, slug=slug)
    if document and not document.is_published:
        document = None
    document_type = (
        document.document_type.value.replace("_", " ").title()
        if document
        else "Legal Document"
    )
    return _render_document(
        request,
        document,
        document_type,
        "Document Not Found",
        "The requested document could not be found.",
    )


def download_document(db: Session, document_id: str):
    document = legal_service.legal_documents.get(db=db, document_id=document_id)
    if not document or not document.is_published:
        return HTMLResponse(content="Document not found", status_code=404)
    try:
        stream, filename = legal_service.legal_documents.stream_file(
            db, document, require_published=True
        )
    except ObjectNotFoundError:
        return HTMLResponse(content="File not found", status_code=404)

    headers = {"Content-Disposition": build_content_disposition(filename)}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers=headers,
    )
