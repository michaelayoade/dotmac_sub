"""Admin legal document management web routes."""

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.legal import LegalDocumentUpdate
from app.services import legal as legal_service
from app.services import web_legal as web_legal_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/legal", tags=["web-admin-legal"])


def _base_context(request: Request, db: Session, active_page: str = "legal"):
    from app.web.admin import get_current_user, get_sidebar_stats

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
    document_type: str | None = None,
    is_published: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all legal documents."""
    context = _base_context(request, db)
    context.update(
        web_legal_service.list_page_data(
            db,
            document_type=document_type,
            is_published=is_published,
            page=page,
            per_page=per_page,
        )
    )
    return templates.TemplateResponse("admin/system/legal/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def legal_document_new(request: Request, db: Session = Depends(get_db)):
    """New legal document form."""
    context = _base_context(request, db)
    context.update({
        "document": None,
        "document_types": web_legal_service.document_type_options(),
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
    summary: str | None = Form(None),
    content: str | None = Form(None),
    is_published: str | None = Form(None),
    effective_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new legal document."""
    try:
        payload = web_legal_service.build_document_create_payload(
            document_type=document_type,
            title=title,
            slug=slug,
            version=version,
            summary=summary,
            content=content,
            is_published=is_published,
            effective_date=effective_date,
        )
        document = legal_service.legal_documents.create(db=db, payload=payload)
        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        context = _base_context(request, db)
        context.update({
            "document": None,
            "document_types": web_legal_service.document_type_options(),
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
        "document_types": web_legal_service.document_type_options(),
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
    summary: str | None = Form(None),
    content: str | None = Form(None),
    is_current: str | None = Form(None),
    is_published: str | None = Form(None),
    effective_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update a legal document."""
    try:
        payload = web_legal_service.build_document_update_payload(
            title=title,
            slug=slug,
            version=version,
            summary=summary,
            content=content,
            is_current=is_current,
            is_published=is_published,
            effective_date=effective_date,
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
            "document_types": web_legal_service.document_type_options(),
            "action": "edit",
            "error": str(e),
        })
        return templates.TemplateResponse(
            "admin/system/legal/form.html", context, status_code=400
        )


@router.post("/{document_id}/upload", response_class=HTMLResponse)
def legal_document_upload(
    request: Request,
    document_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a file for a legal document."""
    try:
        from app.web.admin import get_current_user as get_admin_current_user

        content, file_name, mime_type = web_legal_service.read_and_validate_upload(file)

        current_user = get_admin_current_user(request)
        document = legal_service.legal_documents.upload_file(
            db=db,
            document_id=document_id,
            file_content=content,
            file_name=file_name,
            mime_type=mime_type,
            uploaded_by=current_user.get("subscriber_id") or None,
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
    payload = LegalDocumentUpdate.model_validate(
        {"is_published": True, "is_current": True}
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
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post("/{document_id}/unpublish", response_class=HTMLResponse)
def legal_document_unpublish(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Unpublish a legal document."""
    payload = LegalDocumentUpdate.model_validate({"is_published": False})
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
