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
from app.services.auth_dependencies import require_method_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/network",
    tags=["web-admin-network"],
    dependencies=[
        Depends(require_method_permission("network:tr069:read", "network:tr069:write"))
    ],
)


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
        metadata={
            "name": server.name,
            "base_url": server.base_url,
            "cwmp_url": server.cwmp_url,
        },
    )
    return RedirectResponse(
        "/admin/network/tr069?status=success&message=ACS%20server%20created",
        status_code=303,
    )


@router.get("/tr069/acs/{acs_id}/edit", response_class=HTMLResponse)
def tr069_acs_edit(
    request: Request, acs_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
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
                "acs": web_network_tr069_service.acs_form_snapshot(
                    values, acs_id=acs_id
                ),
                "action_url": f"/admin/network/tr069/acs/{acs_id}",
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/tr069/acs_form.html", context)

    try:
        server = web_network_tr069_service.update_acs_server(
            db, acs_id=acs_id, values=values
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="tr069")
        context.update(
            {
                "acs": web_network_tr069_service.acs_form_snapshot(
                    values, acs_id=acs_id
                ),
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
        metadata={
            "name": server.name,
            "base_url": server.base_url,
            "cwmp_url": server.cwmp_url,
        },
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


@router.get("/tr069/acs/{acs_id}/sync")
def tr069_sync_acs_get_fallback(acs_id: str) -> RedirectResponse:
    """GET fallback for auth-refresh redirects targeting the sync POST endpoint."""
    message = quote_plus("Sync uses POST. Please click Sync again.")
    return RedirectResponse(
        f"/admin/network/tr069?acs_server_id={acs_id}&status=info&message={message}",
        status_code=303,
    )


@router.post("/tr069/devices/{device_id}/link")
def tr069_link_device(
    device_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
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


@router.post("/tr069/devices/{device_id}/create-ont")
def tr069_create_ont(
    device_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""
    try:
        ont, created = web_network_tr069_service.create_ont_from_tr069_device(
            db,
            tr069_device_id=device_id,
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ont",
            entity_id=str(ont.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "source": "tr069_device",
                "tr069_device_id": device_id,
                "created_new": created,
                "serial_number": ont.serial_number,
            },
        )
        return RedirectResponse(
            f"/admin/network/onts/{ont.id}?notice={quote_plus('ONT created from TR-069 device' if created else 'Existing ONT opened from TR-069 match')}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.post("/tr069/devices/{device_id}/action")
def tr069_device_action(
    device_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    form = parse_form_data_sync(request)
    action = str(form.get("action") or "").strip()
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""
    try:
        job = web_network_tr069_service.queue_device_job(
            db, tr069_device_id=device_id, action=action
        )
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


@router.post("/tr069/devices/{device_id}/config")
def tr069_config_push(
    device_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Push configuration to a TR-069 device via setParameterValues."""
    form = parse_form_data_sync(request)
    action_key = str(form.get("config_action") or "").strip()
    value = str(form.get("config_value") or "").strip()
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""

    try:
        job = web_network_tr069_service.create_config_push_job(
            db,
            tr069_device_id=device_id,
            action_key=action_key,
            value=value,
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="config_push",
            entity_type="tr069_device",
            entity_id=device_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "config_action": action_key,
                "job_id": str(job.id),
            },
        )
        message = quote_plus(f"Config pushed: {job.name}")
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


@router.post("/tr069/devices/{device_id}/firmware")
def tr069_firmware_update(
    device_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Push firmware update to a TR-069 device."""
    form = parse_form_data_sync(request)
    firmware_url = str(form.get("firmware_url") or "").strip()
    filename = str(form.get("filename") or "").strip() or None
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""

    try:
        job = web_network_tr069_service.create_firmware_download_job(
            db,
            tr069_device_id=device_id,
            firmware_url=firmware_url,
            filename=filename,
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="firmware_update",
            entity_type="tr069_device",
            entity_id=device_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "firmware_url": firmware_url,
                "filename": filename,
                "job_id": str(job.id),
            },
        )
        message = quote_plus(f"Firmware update queued: {job.name}")
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


@router.post("/tr069/bulk-action")
def tr069_bulk_action(
    request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Execute an action on multiple TR-069 devices."""
    form = parse_form_data_sync(request)
    device_ids_raw = str(form.get("device_ids") or "").strip()
    action = str(form.get("bulk_action") or "").strip()
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""

    # Parse device IDs (comma-separated)
    device_ids = [d.strip() for d in device_ids_raw.split(",") if d.strip()]

    try:
        if not device_ids:
            raise ValueError("No devices selected")
        if not action:
            raise ValueError("No action selected")

        task_id = web_network_tr069_service.queue_bulk_action(
            device_ids=device_ids,
            action=action,
            params=None,
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="bulk_action",
            entity_type="tr069_devices",
            entity_id=task_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "action": action,
                "device_count": len(device_ids),
                "task_id": task_id,
            },
        )
        message = quote_plus(f"Bulk {action} queued for {len(device_ids)} device(s)")
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
