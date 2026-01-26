"""Admin legal document management web routes."""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.legal import LegalDocumentType
from app.schemas.legal import LegalDocumentCreate, LegalDocumentUpdate
from app.services import legal as legal_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/legal", tags=["web-admin-legal"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str = "legal"):
    from app.web.admin import get_sidebar_stats, get_current_user

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("", response_class=HTMLResponse)
def legal_documents_list(
    request: Request,
    document_type: Optional[str] = None,
    is_published: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all legal documents."""
    offset = (page - 1) * per_page

    # Parse filters
    doc_type = None
    if document_type:
        try:
            doc_type = LegalDocumentType(document_type)
        except ValueError:
            pass

    published = None
    if is_published == "true":
        published = True
    elif is_published == "false":
        published = False

    documents = legal_service.legal_documents.list(
        db=db,
        document_type=doc_type,
        is_published=published,
        order_by="updated_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    # Get all documents for count
    all_docs = legal_service.legal_documents.list(
        db=db,
        document_type=doc_type,
        is_published=published,
        limit=1000,
        offset=0,
    )
    total = len(all_docs)
    total_pages = (total + per_page - 1) // per_page

    stats = {
        "total": total,
        "published": sum(1 for d in all_docs if d.is_published),
        "draft": sum(1 for d in all_docs if not d.is_published),
    }

    context = _base_context(request, db)
    context.update({
        "documents": documents,
        "stats": stats,
        "document_types": [t.value for t in LegalDocumentType],
        "document_type_filter": document_type,
        "is_published_filter": is_published,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })

    return templates.TemplateResponse("admin/system/legal/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def legal_document_new(request: Request, db: Session = Depends(get_db)):
    """New legal document form."""
    context = _base_context(request, db)
    context.update({
        "document": None,
        "document_types": [(t.value, t.value.replace("_", " ").title()) for t in LegalDocumentType],
        "action": "create",
    })
    return templates.TemplateResponse("admin/system/legal/form.html", context)


@router.post("/new", response_class=HTMLResponse)
def legal_document_create(
    request: Request,
    document_type: str = Form(...),
    title: str = Form(...),
    slug: str = Form(...),
    version: str = Form("1.0"),
    summary: Optional[str] = Form(None),
    content: Optional[str] = Form(None),
    is_published: Optional[str] = Form(None),
    effective_date: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new legal document."""
    try:
        doc_type = LegalDocumentType(document_type)
        eff_date = None
        if effective_date:
            eff_date = datetime.fromisoformat(effective_date.replace("Z", "+00:00"))

        payload = LegalDocumentCreate(
            document_type=doc_type,
            title=title,
            slug=slug,
            version=version,
            summary=summary if summary else None,
            content=content if content else None,
            is_published=is_published == "true",
            effective_date=eff_date,
        )

        document = legal_service.legal_documents.create(db=db, payload=payload)
        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        context = _base_context(request, db)
        context.update({
            "document": None,
            "document_types": [(t.value, t.value.replace("_", " ").title()) for t in LegalDocumentType],
            "action": "create",
            "error": str(e),
        })
        return templates.TemplateResponse(
            "admin/system/legal/form.html", context, status_code=400
        )


@router.get("/{document_id}", response_class=HTMLResponse)
def legal_document_detail(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """View legal document details."""
    document = legal_service.legal_documents.get(db=db, document_id=document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )

    context = _base_context(request, db)
    context.update({"document": document})
    return templates.TemplateResponse("admin/system/legal/detail.html", context)


@router.get("/{document_id}/edit", response_class=HTMLResponse)
def legal_document_edit(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Edit legal document form."""
    document = legal_service.legal_documents.get(db=db, document_id=document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )

    context = _base_context(request, db)
    context.update({
        "document": document,
        "document_types": [(t.value, t.value.replace("_", " ").title()) for t in LegalDocumentType],
        "action": "edit",
    })
    return templates.TemplateResponse("admin/system/legal/form.html", context)


@router.post("/{document_id}/edit", response_class=HTMLResponse)
def legal_document_update(
    request: Request,
    document_id: str,
    title: str = Form(...),
    slug: str = Form(...),
    version: str = Form("1.0"),
    summary: Optional[str] = Form(None),
    content: Optional[str] = Form(None),
    is_current: Optional[str] = Form(None),
    is_published: Optional[str] = Form(None),
    effective_date: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Update a legal document."""
    try:
        eff_date = None
        if effective_date:
            eff_date = datetime.fromisoformat(effective_date.replace("Z", "+00:00"))

        payload = LegalDocumentUpdate(
            title=title,
            slug=slug,
            version=version,
            summary=summary if summary else None,
            content=content if content else None,
            is_current=is_current == "true",
            is_published=is_published == "true",
            effective_date=eff_date,
        )

        document = legal_service.legal_documents.update(
            db=db, document_id=document_id, payload=payload
        )
        if not document:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "Document not found"},
                status_code=404,
            )

        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        document = legal_service.legal_documents.get(db=db, document_id=document_id)
        context = _base_context(request, db)
        context.update({
            "document": document,
            "document_types": [(t.value, t.value.replace("_", " ").title()) for t in LegalDocumentType],
            "action": "edit",
            "error": str(e),
        })
        return templates.TemplateResponse(
            "admin/system/legal/form.html", context, status_code=400
        )


@router.post("/{document_id}/upload", response_class=HTMLResponse)
async def legal_document_upload(
    request: Request,
    document_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a file for a legal document."""
    try:
        # Validate file type
        allowed_types = [
            "application/pdf",
            "text/html",
            "text/plain",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ]
        if file.content_type not in allowed_types:
            raise ValueError(
                f"File type not allowed. Allowed types: PDF, HTML, TXT, DOC, DOCX"
            )

        # Read file content
        content = await file.read()

        # Max file size: 10MB
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("File size exceeds 10MB limit")

        document = legal_service.legal_documents.upload_file(
            db=db,
            document_id=document_id,
            file_content=content,
            file_name=file.filename,
            mime_type=file.content_type,
        )

        if not document:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "Document not found"},
                status_code=404,
            )

        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        context = _base_context(request, db)
        document = legal_service.legal_documents.get(db=db, document_id=document_id)
        context.update({"document": document, "error": str(e)})
        return templates.TemplateResponse(
            "admin/system/legal/detail.html", context, status_code=400
        )


@router.post("/{document_id}/delete-file", response_class=HTMLResponse)
def legal_document_delete_file(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Delete the file associated with a legal document."""
    document = legal_service.legal_documents.delete_file(db=db, document_id=document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post("/{document_id}/publish", response_class=HTMLResponse)
def legal_document_publish(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Publish a legal document."""
    payload = LegalDocumentUpdate(is_published=True, is_current=True)
    document = legal_service.legal_documents.update(
        db=db, document_id=document_id, payload=payload
    )
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post("/{document_id}/unpublish", response_class=HTMLResponse)
def legal_document_unpublish(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Unpublish a legal document."""
    payload = LegalDocumentUpdate(is_published=False)
    document = legal_service.legal_documents.update(
        db=db, document_id=document_id, payload=payload
    )
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post("/{document_id}/delete", response_class=HTMLResponse)
def legal_document_delete(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Delete a legal document."""
    success = legal_service.legal_documents.delete(db=db, document_id=document_id)
    if not success:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    return RedirectResponse(url="/admin/system/legal", status_code=303)
