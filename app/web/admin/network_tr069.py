"""Admin network TR-069 (ACS) web routes."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_tr069 as web_network_tr069_service
from app.services.audit_helpers import log_audit_event
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/tr069", response_class=HTMLResponse)
def tr069_dashboard(
    request: Request,
    acs_server_id: str | None = None,
    search: str | None = None,
    only_unlinked: bool = False,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_network_tr069_service.tr069_dashboard_data(
        db,
        acs_server_id=acs_server_id,
        search=search,
        only_unlinked=only_unlinked,
    )
    context = _base_context(request, db, active_page="tr069")
    context.update(page_data)
    context["status"] = status
    context["message"] = message
    return templates.TemplateResponse("admin/network/tr069/index.html", context)


@router.get("/tr069/acs/new", response_class=HTMLResponse)
def tr069_acs_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="tr069")
    context.update(
        {
            "acs": web_network_tr069_service.acs_form_snapshot({"is_active": True}),
            "action_url": "/admin/network/tr069/acs",
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)


@router.post("/tr069/acs", response_class=HTMLResponse)
def tr069_acs_create(request: Request, db: Session = Depends(get_db)):
    values = web_network_tr069_service.parse_acs_form(parse_form_data_sync(request))
    error = web_network_tr069_service.validate_acs_values(values)
    if error:
        context = _base_context(request, db, active_page="tr069")
        context.update(
            {
                "acs": web_network_tr069_service.acs_form_snapshot(values),
                "action_url": "/admin/network/tr069/acs",
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)

    try:
        server = web_network_tr069_service.create_acs_server(db, values)
    except Exception as exc:
        context = _base_context(request, db, active_page="tr069")
        context.update(
            {
                "acs": web_network_tr069_service.acs_form_snapshot(values),
                "action_url": "/admin/network/tr069/acs",
                "error": str(exc),
            }
        )
        return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="tr069_acs_server",
        entity_id=str(server.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": server.name, "base_url": server.base_url},
    )
    return RedirectResponse(
        "/admin/network/tr069?status=success&message=ACS%20server%20created",
        status_code=303,
    )


@router.get("/tr069/acs/{acs_id}/edit", response_class=HTMLResponse)
def tr069_acs_edit(request: Request, acs_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        server = web_network_tr069_service.get_acs_server(db, acs_id=acs_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ACS server not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="tr069")
    context.update(
        {
            "acs": web_network_tr069_service.acs_form_snapshot_from_model(server),
            "action_url": f"/admin/network/tr069/acs/{acs_id}",
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)


@router.post("/tr069/acs/{acs_id}", response_class=HTMLResponse)
def tr069_acs_update(request: Request, acs_id: str, db: Session = Depends(get_db)):
    values = web_network_tr069_service.parse_acs_form(parse_form_data_sync(request))
    error = web_network_tr069_service.validate_acs_values(values)
    if error:
        context = _base_context(request, db, active_page="tr069")
        context.update(
            {
                "acs": web_network_tr069_service.acs_form_snapshot(values, acs_id=acs_id),
                "action_url": f"/admin/network/tr069/acs/{acs_id}",
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)

    try:
        server = web_network_tr069_service.update_acs_server(db, acs_id=acs_id, values=values)
    except Exception as exc:
        context = _base_context(request, db, active_page="tr069")
        context.update(
            {
                "acs": web_network_tr069_service.acs_form_snapshot(values, acs_id=acs_id),
                "action_url": f"/admin/network/tr069/acs/{acs_id}",
                "error": str(exc),
            }
        )
        return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="tr069_acs_server",
        entity_id=str(server.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": server.name, "base_url": server.base_url},
    )
    return RedirectResponse(
        "/admin/network/tr069?status=success&message=ACS%20server%20updated",
        status_code=303,
    )


@router.post("/tr069/acs/{acs_id}/sync")
def tr069_sync_acs(acs_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        result = web_network_tr069_service.sync_server(db, acs_server_id=acs_id)
        message = quote_plus(
            f"Sync complete: created={result.get('created', 0)}, updated={result.get('updated', 0)}"
        )
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_id}&status=error&message={message}",
            status_code=303,
        )


@router.post("/tr069/devices/{device_id}/link")
def tr069_link_device(device_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = parse_form_data_sync(request)
    cpe_device_id = str(form.get("cpe_device_id") or "").strip() or None
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""
    try:
        web_network_tr069_service.link_tr069_device_to_cpe(
            db,
            tr069_device_id=device_id,
            cpe_device_id=cpe_device_id,
        )
        message = quote_plus("Device link updated")
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.post("/tr069/devices/{device_id}/action")
def tr069_device_action(device_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = parse_form_data_sync(request)
    action = str(form.get("action") or "").strip()
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""
    try:
        job = web_network_tr069_service.queue_device_job(db, tr069_device_id=device_id, action=action)
        message = quote_plus(f"Action queued: {job.command}")
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )
