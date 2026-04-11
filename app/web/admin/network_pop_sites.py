"""Admin network POP sites web routes."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_pop_sites as web_network_pop_sites_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/pop-sites",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def pop_sites_list(
    request: Request, status: str | None = None, db: Session = Depends(get_db)
):
    """List all POP sites."""
    page_data = web_network_pop_sites_service.list_page_data(db, status)
    context = _base_context(request, db, active_page="pop-sites")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/pop-sites/index.html", context)


@router.get(
    "/pop-sites/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def pop_site_new(request: Request, db: Session = Depends(get_db)):
    reference_data = web_network_pop_sites_service.form_reference_data(db)
    form_context = web_network_pop_sites_service.build_form_context(
        pop_site=None,
        action_url="/admin/network/pop-sites",
        mast_enabled=False,
        mast_defaults=web_network_pop_sites_service.default_mast_context(),
        reference_data=reference_data,
    )
    context = _base_context(request, db, active_page="pop-sites")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/pop-sites/form.html", context)


@router.post(
    "/pop-sites",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    result = web_network_pop_sites_service.create_site_from_form(
        db, form, request=request
    )
    if result.error:
        context = _base_context(request, db, active_page="pop-sites")
        context.update(result.form_context or {})
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    pop_site = result.pop_site
    if pop_site is None:
        raise HTTPException(status_code=500, detail="POP site creation failed")
    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get(
    "/pop-sites/{pop_site_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def pop_site_edit(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )
    reference_data = web_network_pop_sites_service.form_reference_data(db)
    form_context = web_network_pop_sites_service.build_form_context(
        pop_site=pop_site,
        action_url=f"/admin/network/pop-sites/{pop_site.id}",
        mast_enabled=False,
        mast_defaults=web_network_pop_sites_service.default_mast_context(),
        reference_data=reference_data,
    )
    context = _base_context(request, db, active_page="pop-sites")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/pop-sites/form.html", context)


@router.post(
    "/pop-sites/{pop_site_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_update(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    result = web_network_pop_sites_service.update_site_from_form(
        db,
        pop_site_id,
        form,
        request=request,
    )
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )
    if result.error:
        context = _base_context(request, db, active_page="pop-sites")
        context.update(result.form_context or {})
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    pop_site = result.pop_site
    if pop_site is None:
        raise HTTPException(status_code=500, detail="POP site update failed")
    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get(
    "/pop-sites/{pop_site_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def pop_site_detail(
    request: Request,
    pop_site_id: str,
    tab: str = "information",
    db: Session = Depends(get_db),
):
    page_data = web_network_pop_sites_service.detail_page_data(db, pop_site_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    if tab not in {
        "information",
        "hardware",
        "customer-services",
        "map",
        "gallery",
        "documents",
        "contacts",
    }:
        tab = "information"

    activities = build_audit_activities(db, "pop_site", str(pop_site_id))
    context = _base_context(request, db, active_page="pop-sites")
    context.update(page_data)
    context["active_tab"] = tab
    context["activities"] = activities
    return templates.TemplateResponse("admin/network/pop-sites/detail.html", context)


@router.post(
    "/pop-sites/{pop_site_id}/photos",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_photo_upload(
    request: Request,
    pop_site_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    result = web_network_pop_sites_service.upload_photo(
        db,
        pop_site_id=pop_site_id,
        file=file,
        request=request,
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="POP Site not found")
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab=gallery",
        status_code=303,
    )


@router.post(
    "/pop-sites/{pop_site_id}/documents",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_document_upload(
    request: Request,
    pop_site_id: str,
    category: str = Form("other"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    result = web_network_pop_sites_service.upload_document(
        db,
        pop_site_id=pop_site_id,
        category=category,
        file=file,
        request=request,
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="POP Site not found")
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab=documents",
        status_code=303,
    )


@router.get(
    "/pop-sites/{pop_site_id}/files/{file_id}/download",
    dependencies=[Depends(require_permission("network:read"))],
)
def pop_site_file_download(
    request: Request,
    pop_site_id: str,
    file_id: str,
    db: Session = Depends(get_db),
):
    stream = web_network_pop_sites_service.stream_site_file(
        db,
        pop_site_id=pop_site_id,
        file_id=file_id,
        request=request,
        as_attachment=True,
    )
    if stream.not_found or stream.chunks is None:
        raise HTTPException(status_code=404, detail="File not found")
    return StreamingResponse(
        stream.chunks,
        media_type=stream.media_type,
        headers=stream.headers or {},
    )


@router.get(
    "/pop-sites/{pop_site_id}/files/{file_id}/preview",
    dependencies=[Depends(require_permission("network:read"))],
)
def pop_site_file_preview(
    request: Request,
    pop_site_id: str,
    file_id: str,
    db: Session = Depends(get_db),
):
    stream = web_network_pop_sites_service.stream_site_file(
        db,
        pop_site_id=pop_site_id,
        file_id=file_id,
        request=request,
    )
    if stream.not_found or stream.chunks is None:
        raise HTTPException(status_code=404, detail="File not found")
    return StreamingResponse(
        stream.chunks,
        media_type=stream.media_type,
        headers=stream.headers or {},
    )


@router.post(
    "/pop-sites/{pop_site_id}/files/{file_id}/delete",
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_file_delete(
    request: Request,
    pop_site_id: str,
    file_id: str,
    tab: str = Form("documents"),
    db: Session = Depends(get_db),
):
    deleted = web_network_pop_sites_service.delete_site_file(
        db,
        pop_site_id=pop_site_id,
        file_id=file_id,
        request=request,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    if tab not in {"gallery", "documents"}:
        tab = "documents"
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab={tab}",
        status_code=303,
    )


@router.post(
    "/pop-sites/{pop_site_id}/contacts",
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_contact_create(
    pop_site_id: str,
    name: str = Form(...),
    role: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    is_primary: bool = Form(False),
    db: Session = Depends(get_db),
):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        raise HTTPException(status_code=404, detail="POP Site not found")
    if not name.strip():
        return RedirectResponse(
            f"/admin/network/pop-sites/{pop_site_id}?tab=contacts",
            status_code=303,
        )
    web_network_pop_sites_service.create_contact(
        db,
        pop_site_id=pop_site_id,
        name=name.strip(),
        role=role.strip() or None,
        phone=phone.strip() or None,
        email=email.strip() or None,
        notes=notes.strip() or None,
        is_primary=is_primary,
    )
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab=contacts",
        status_code=303,
    )


@router.post(
    "/pop-sites/{pop_site_id}/contacts/{contact_id}/delete",
    dependencies=[Depends(require_permission("network:write"))],
)
def pop_site_contact_delete(
    pop_site_id: str,
    contact_id: str,
    db: Session = Depends(get_db),
):
    web_network_pop_sites_service.delete_contact(
        db,
        pop_site_id=pop_site_id,
        contact_id=contact_id,
    )
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab=contacts",
        status_code=303,
    )


# ==================== Fiber Plant (ODN) ====================
