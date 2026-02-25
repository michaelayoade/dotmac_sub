"""Admin network POP sites web routes."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services import web_network_pop_sites as web_network_pop_sites_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.services.file_storage import build_content_disposition, file_uploads
from app.services.object_storage import ObjectNotFoundError
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

_coerce_float_or_none = web_network_core_runtime_service.coerce_float_or_none


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }

@router.get("/pop-sites", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def pop_sites_list(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    """List all POP sites."""
    page_data = web_network_pop_sites_service.list_page_data(db, status)
    context = _base_context(request, db, active_page="pop-sites")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/pop-sites/index.html", context)


@router.get("/pop-sites/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.post("/pop-sites", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def pop_site_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    reference_data = web_network_pop_sites_service.form_reference_data(db)
    values = web_network_pop_sites_service.parse_site_form_values(form)
    normalized, error = web_network_pop_sites_service.validate_site_values(values)
    lat_value = _coerce_float_or_none(normalized.get("latitude")) if normalized else None
    lon_value = _coerce_float_or_none(normalized.get("longitude")) if normalized else None
    mast_enabled, mast_data, mast_error, mast_defaults = web_network_pop_sites_service.parse_mast_form(
        form, lat_value, lon_value
    )

    if error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=None,
            action_url="/admin/network/pop-sites",
            error=error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
            reference_data=reference_data,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)
    if mast_error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=None,
            action_url="/admin/network/pop-sites",
            mast_error=mast_error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
            reference_data=reference_data,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    assert normalized is not None
    normalized, error = web_network_pop_sites_service.resolve_site_relationships(db, normalized)
    if error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=None,
            action_url="/admin/network/pop-sites",
            error=error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
            reference_data=reference_data,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)
    assert normalized is not None
    pop_site = web_network_pop_sites_service.create_site(db, normalized)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="pop_site",
        entity_id=str(pop_site.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": pop_site.name, "code": pop_site.code},
    )

    if mast_enabled:
        web_network_pop_sites_service.maybe_create_mast(db, str(pop_site.id), mast_data)

    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get("/pop-sites/{pop_site_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.post("/pop-sites/{pop_site_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def pop_site_update(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    reference_data = web_network_pop_sites_service.form_reference_data(db)
    values = web_network_pop_sites_service.parse_site_form_values(form)
    normalized, error = web_network_pop_sites_service.validate_site_values(values)
    fallback_lat = (
        _coerce_float_or_none(normalized.get("latitude")) if normalized else pop_site.latitude
    )
    fallback_lon = (
        _coerce_float_or_none(normalized.get("longitude")) if normalized else pop_site.longitude
    )
    mast_enabled, mast_data, mast_error, mast_defaults = web_network_pop_sites_service.parse_mast_form(
        form, fallback_lat, fallback_lon
    )

    if error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=pop_site,
            action_url=f"/admin/network/pop-sites/{pop_site.id}",
            error=error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
            reference_data=reference_data,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    assert normalized is not None
    if mast_error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=pop_site,
            action_url=f"/admin/network/pop-sites/{pop_site.id}",
            mast_error=mast_error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
            reference_data=reference_data,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    normalized, error = web_network_pop_sites_service.resolve_site_relationships(db, normalized)
    if error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=pop_site,
            action_url=f"/admin/network/pop-sites/{pop_site.id}",
            error=error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
            reference_data=reference_data,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)
    assert normalized is not None

    before_snapshot = model_to_dict(pop_site)
    web_network_pop_sites_service.commit_site_update(db, pop_site, normalized)
    after_snapshot = model_to_dict(pop_site)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="pop_site",
        entity_id=str(pop_site.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )

    if mast_enabled:
        web_network_pop_sites_service.maybe_create_mast(db, str(pop_site.id), mast_data)

    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get("/pop-sites/{pop_site_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.post("/pop-sites/{pop_site_id}/photos", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def pop_site_photo_upload(
    request: Request,
    pop_site_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        raise HTTPException(status_code=404, detail="POP Site not found")
    if not file.filename:
        return RedirectResponse(
            f"/admin/network/pop-sites/{pop_site_id}?tab=gallery",
            status_code=303,
        )
    payload = file.file.read()
    if not payload:
        return RedirectResponse(
            f"/admin/network/pop-sites/{pop_site_id}?tab=gallery",
            status_code=303,
        )
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = current_user.get("subscriber_id")
    organization_id = (
        file_uploads.resolve_user_organization(db, actor_id) if actor_id else None
    )
    file_uploads.upload(
        db=db,
        domain="branding",
        entity_type="pop_site_photo",
        entity_id=str(pop_site_id),
        original_filename=file.filename,
        content_type=file.content_type,
        data=payload,
        uploaded_by=actor_id,
        organization_id=organization_id,
    )
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab=gallery",
        status_code=303,
    )


@router.post("/pop-sites/{pop_site_id}/documents", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def pop_site_document_upload(
    request: Request,
    pop_site_id: str,
    category: str = Form("other"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        raise HTTPException(status_code=404, detail="POP Site not found")
    if category not in {"lease", "permit", "survey", "asbuilt", "other"}:
        category = "other"
    if not file.filename:
        return RedirectResponse(
            f"/admin/network/pop-sites/{pop_site_id}?tab=documents",
            status_code=303,
        )
    payload = file.file.read()
    if not payload:
        return RedirectResponse(
            f"/admin/network/pop-sites/{pop_site_id}?tab=documents",
            status_code=303,
        )
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = current_user.get("subscriber_id")
    organization_id = (
        file_uploads.resolve_user_organization(db, actor_id) if actor_id else None
    )
    file_uploads.upload(
        db=db,
        domain="attachments",
        entity_type=f"pop_site_document_{category}",
        entity_id=str(pop_site_id),
        original_filename=file.filename,
        content_type=file.content_type,
        data=payload,
        uploaded_by=actor_id,
        organization_id=organization_id,
    )
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab=documents",
        status_code=303,
    )


@router.get("/pop-sites/{pop_site_id}/files/{file_id}/download", dependencies=[Depends(require_permission("network:read"))])
def pop_site_file_download(
    request: Request,
    pop_site_id: str,
    file_id: str,
    db: Session = Depends(get_db),
):
    record = web_network_pop_sites_service.get_site_file_or_none(db, file_id)
    if not record or record.entity_id != str(pop_site_id):
        raise HTTPException(status_code=404, detail="File not found")
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    current_org = (
        file_uploads.resolve_user_organization(db, current_user.get("subscriber_id"))
        if current_user.get("subscriber_id")
        else None
    )
    file_uploads.assert_tenant_access(record, current_org)
    try:
        stream = file_uploads.stream_file(record)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc

    headers = {"Content-Disposition": build_content_disposition(record.original_filename)}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers=headers,
    )


@router.get("/pop-sites/{pop_site_id}/files/{file_id}/preview", dependencies=[Depends(require_permission("network:read"))])
def pop_site_file_preview(
    request: Request,
    pop_site_id: str,
    file_id: str,
    db: Session = Depends(get_db),
):
    record = web_network_pop_sites_service.get_site_file_or_none(db, file_id)
    if not record or record.entity_id != str(pop_site_id):
        raise HTTPException(status_code=404, detail="File not found")
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    current_org = (
        file_uploads.resolve_user_organization(db, current_user.get("subscriber_id"))
        if current_user.get("subscriber_id")
        else None
    )
    file_uploads.assert_tenant_access(record, current_org)
    try:
        stream = file_uploads.stream_file(record)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
    )


@router.post("/pop-sites/{pop_site_id}/files/{file_id}/delete", dependencies=[Depends(require_permission("network:write"))])
def pop_site_file_delete(
    request: Request,
    pop_site_id: str,
    file_id: str,
    tab: str = Form("documents"),
    db: Session = Depends(get_db),
):
    record = web_network_pop_sites_service.get_site_file_or_none(db, file_id)
    if not record or record.entity_id != str(pop_site_id):
        raise HTTPException(status_code=404, detail="File not found")
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    current_org = (
        file_uploads.resolve_user_organization(db, current_user.get("subscriber_id"))
        if current_user.get("subscriber_id")
        else None
    )
    file_uploads.assert_tenant_access(record, current_org)
    file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
    if tab not in {"gallery", "documents"}:
        tab = "documents"
    return RedirectResponse(
        f"/admin/network/pop-sites/{pop_site_id}?tab={tab}",
        status_code=303,
    )


@router.post("/pop-sites/{pop_site_id}/contacts", dependencies=[Depends(require_permission("network:write"))])
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


@router.post("/pop-sites/{pop_site_id}/contacts/{contact_id}/delete", dependencies=[Depends(require_permission("network:write"))])
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
