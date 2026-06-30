"""Admin legal document management web routes."""

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_legal as web_legal_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/legal", tags=["web-admin-legal"])


def _legal_metadata(document, *, extra: dict | None = None) -> dict:
    metadata = {
        "document_type": getattr(
            getattr(document, "document_type", None), "value", None
        ),
        "title": getattr(document, "title", None),
        "slug": getattr(document, "slug", None),
        "version": getattr(document, "version", None),
        "is_published": bool(getattr(document, "is_published", False)),
        "is_current": bool(getattr(document, "is_current", False)),
    }
    if extra:
        metadata.update(extra)
    return metadata


def _log_legal_event(
    db: Session,
    request: Request,
    *,
    action: str,
    document,
    extra: dict | None = None,
) -> None:
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="legal_document",
        entity_id=str(getattr(document, "id", "")) if document else None,
        actor_id=None,
        metadata=_legal_metadata(document, extra=extra) if document else extra,
    )


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def legal_documents_list(
    request: Request,
    document_type: str | None = None,
    is_published: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all legal documents."""
    context = web_legal_service.list_context(
        request,
        db,
        document_type=document_type,
        is_published=is_published,
        page=page,
        per_page=per_page,
    )
    return templates.TemplateResponse("admin/system/legal/index.html", context)


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_new(request: Request, db: Session = Depends(get_db)):
    """New legal document form."""
    context = web_legal_service.form_context(request, db)
    return templates.TemplateResponse("admin/system/legal/form.html", context)


@router.post(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
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
        document = web_legal_service.create_document(
            db,
            document_type=document_type,
            title=title,
            slug=slug,
            version=version,
            summary=summary,
            content=content,
            is_published=is_published,
            effective_date=effective_date,
        )
        _log_legal_event(
            db,
            request,
            action="create",
            document=document,
            extra={"published_on_create": bool(document.is_published)},
        )
        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        context = web_legal_service.form_context(request, db, error=str(e))
        return templates.TemplateResponse(
            "admin/system/legal/form.html", context, status_code=400
        )


@router.get(
    "/{document_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:read"))],
)
def legal_document_detail(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """View legal document details."""
    document = web_legal_service.get_document(db, document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )

    context = web_legal_service.detail_context(request, db, document=document)
    return templates.TemplateResponse("admin/system/legal/detail.html", context)


@router.get(
    "/{document_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_edit(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Edit legal document form."""
    document = web_legal_service.get_document(db, document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )

    context = web_legal_service.form_context(
        request,
        db,
        document=document,
        action="edit",
    )
    return templates.TemplateResponse("admin/system/legal/form.html", context)


@router.post(
    "/{document_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
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
        document = web_legal_service.update_document(
            db,
            document_id=document_id,
            title=title,
            slug=slug,
            version=version,
            summary=summary,
            content=content,
            is_current=is_current,
            is_published=is_published,
            effective_date=effective_date,
        )
        if not document:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "Document not found"},
                status_code=404,
            )

        _log_legal_event(db, request, action="update", document=document)
        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        document = web_legal_service.get_document(db, document_id)
        context = web_legal_service.form_context(
            request,
            db,
            document=document,
            action="edit",
            error=str(e),
        )
        return templates.TemplateResponse(
            "admin/system/legal/form.html", context, status_code=400
        )


@router.post(
    "/{document_id}/upload",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_upload(
    request: Request,
    document_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a file for a legal document."""
    try:
        document = web_legal_service.upload_document_file(
            request,
            db,
            document_id=document_id,
            file_content=file.file.read(),
            file_name=file.filename or "document",
            mime_type=file.content_type,
        )

        if not document:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "Document not found"},
                status_code=404,
            )

        _log_legal_event(
            db,
            request,
            action="upload_file",
            document=document,
            extra={"file_name": document.file_name, "file_size": document.file_size},
        )
        return RedirectResponse(
            url=f"/admin/system/legal/{document.id}", status_code=303
        )
    except Exception as e:
        document = web_legal_service.get_document(db, document_id)
        context = web_legal_service.detail_context(request, db, document=document)
        context["error"] = str(e)
        return templates.TemplateResponse(
            "admin/system/legal/detail.html", context, status_code=400
        )


@router.post(
    "/{document_id}/delete-file",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_delete_file(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Delete the file associated with a legal document."""
    existing = web_legal_service.get_document(db, document_id)
    file_name = getattr(existing, "file_name", None) if existing else None
    document = web_legal_service.delete_document_file(db, document_id=document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    _log_legal_event(
        db,
        request,
        action="delete_file",
        document=document,
        extra={"file_name": file_name},
    )
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post(
    "/{document_id}/publish",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_publish(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Publish a legal document."""
    document = web_legal_service.publish_document(db, document_id=document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    _log_legal_event(db, request, action="publish", document=document)
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post(
    "/{document_id}/unpublish",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_unpublish(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Unpublish a legal document."""
    document = web_legal_service.unpublish_document(db, document_id=document_id)
    if not document:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    _log_legal_event(db, request, action="unpublish", document=document)
    return RedirectResponse(url=f"/admin/system/legal/{document.id}", status_code=303)


@router.post(
    "/{document_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:write"))],
)
def legal_document_delete(
    request: Request, document_id: str, db: Session = Depends(get_db)
):
    """Delete a legal document."""
    document = web_legal_service.get_document(db, document_id)
    success = web_legal_service.delete_document(db, document_id=document_id)
    if not success:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Document not found"},
            status_code=404,
        )
    _log_legal_event(db, request, action="delete", document=document)
    return RedirectResponse(url="/admin/system/legal", status_code=303)
