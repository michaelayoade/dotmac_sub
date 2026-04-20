"""Admin network TR-069 (ACS) web routes."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_tr069 as web_network_tr069_service
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
        server = web_network_tr069_service.create_acs_server(
            db, values, request=request
        )
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
            db, acs_id=acs_id, values=values, request=request
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


@router.get("/tr069/tasks", response_class=HTMLResponse)
def tr069_acs_tasks(
    request: Request,
    acs_server_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="tr069")
    context.update(
        web_network_tr069_service.acs_task_console_data(
            db,
            acs_server_id=acs_server_id,
        )
    )
    context["status"] = status
    context["message"] = message
    return templates.TemplateResponse("admin/network/tr069/tasks.html", context)


@router.post("/tr069/tasks/{task_id}/delete")
def tr069_delete_acs_task(
    task_id: str,
    request: Request,
    acs_server_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        web_network_tr069_service.delete_acs_task(
            db,
            acs_server_id=acs_server_id,
            task_id=task_id,
            request=request,
        )
        message = quote_plus("Pending ACS task deleted")
        return RedirectResponse(
            f"/admin/network/tr069/tasks?acs_server_id={acs_server_id}&status=success&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/tasks?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


@router.post("/tr069/tasks/clear")
def tr069_clear_acs_tasks(
    request: Request,
    acs_server_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        result = web_network_tr069_service.clear_acs_tasks(
            db,
            acs_server_id=acs_server_id,
            request=request,
        )
        deleted = int(result.get("deleted") or 0)
        errors = result.get("errors") or []
        if errors:
            message = quote_plus(
                f"Deleted {deleted} pending task(s); {len(errors)} task(s) failed"
            )
            status = "error"
        else:
            message = quote_plus(f"Deleted {deleted} pending ACS task(s)")
            status = "success"
        return RedirectResponse(
            f"/admin/network/tr069/tasks?acs_server_id={acs_server_id}&status={status}&message={message}",
            status_code=303,
        )
    except Exception as exc:
        message = quote_plus(str(exc))
        return RedirectResponse(
            f"/admin/network/tr069/tasks?acs_server_id={acs_server_id}&status=error&message={message}",
            status_code=303,
        )


# -----------------------------------------------------------------------------
# ACS Enforcement Preset Management
# -----------------------------------------------------------------------------


@router.get("/tr069/acs/{acs_id}/enforcement-status")
def tr069_acs_enforcement_status(acs_id: str, db: Session = Depends(get_db)) -> dict:
    """Get ACS enforcement preset status (JSON response for HTMX)."""
    from app.services.tr069 import get_acs_enforcement_status

    try:
        return get_acs_enforcement_status(db, acs_id)
    except Exception as exc:
        return {"exists": False, "error": str(exc)}


@router.post("/tr069/acs/{acs_id}/enforcement-preset")
def tr069_push_enforcement_preset(
    acs_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Push ACS enforcement preset to GenieACS.

    This creates a provision and preset in GenieACS that enforces this
    ACS server's URL on every device inform (bootstrap, boot, periodic).
    """
    form = parse_form_data_sync(request)
    on_bootstrap = str(form.get("on_bootstrap", "1")).strip() in ("1", "true", "on")
    on_boot = str(form.get("on_boot", "1")).strip() in ("1", "true", "on")
    on_periodic = str(form.get("on_periodic", "1")).strip() in ("1", "true", "on")
    precondition = str(form.get("precondition") or "").strip()

    try:
        result = web_network_tr069_service.push_acs_enforcement_preset(
            db,
            acs_id,
            on_bootstrap=on_bootstrap,
            on_boot=on_boot,
            on_periodic=on_periodic,
            precondition=precondition,
            request=request,
        )
        message = quote_plus(
            f"ACS enforcement preset created: {result.get('preset_id')}"
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


@router.post("/tr069/acs/{acs_id}/enforcement-preset/remove")
def tr069_remove_enforcement_preset(
    acs_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Remove ACS enforcement preset from GenieACS."""
    try:
        result = web_network_tr069_service.remove_acs_enforcement_preset(
            db, acs_id, request=request
        )
        message = quote_plus("ACS enforcement preset removed")
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


# -----------------------------------------------------------------------------
# Runtime Data Collection Preset
# -----------------------------------------------------------------------------


@router.get("/tr069/acs/{acs_id}/runtime-status")
def tr069_acs_runtime_status(acs_id: str, db: Session = Depends(get_db)) -> dict:
    """Get runtime collection preset status (JSON response for HTMX)."""
    from app.services.tr069 import get_runtime_collection_status

    try:
        return get_runtime_collection_status(db, acs_id)
    except Exception as exc:
        return {"exists": False, "error": str(exc)}


@router.post("/tr069/acs/{acs_id}/runtime-preset")
def tr069_push_runtime_preset(
    acs_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Push runtime data collection preset to GenieACS.

    This creates a provision and preset that collects operational parameters
    (WiFi clients, WAN status, PPPoE status, LAN mode) on device inform.
    """
    try:
        result = web_network_tr069_service.push_runtime_collection_preset(
            db, acs_id, request=request
        )
        message = quote_plus(
            f"Runtime collection preset created: {result.get('preset_id')}"
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
            request=request,
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
            request=request,
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
            request=request,
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
            db,
            device_ids=device_ids,
            action=action,
            params=None,
            request=request,
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


@router.post("/tr069/devices/{device_id}/nat-forward")
def tr069_nat_forward(
    device_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Create a NAT port forwarding rule on a TR-069 device."""
    form = parse_form_data_sync(request)
    acs_server_id = str(form.get("acs_server_id") or "").strip() or ""

    try:
        external_port = int(str(form.get("external_port") or "0").strip())
        internal_ip = str(form.get("internal_ip") or "").strip()
        internal_port = int(str(form.get("internal_port") or "0").strip())
        protocol = str(form.get("protocol") or "TCP").strip()
        description = str(form.get("description") or "").strip() or None

        if not external_port or not internal_ip or not internal_port:
            raise ValueError(
                "External port, internal IP, and internal port are required"
            )

        job = web_network_tr069_service.create_nat_port_forward_job(
            db,
            tr069_device_id=device_id,
            external_port=external_port,
            internal_ip=internal_ip,
            internal_port=internal_port,
            protocol=protocol,
            description=description,
            request=request,
        )
        message = quote_plus(
            f"NAT forward rule queued: {external_port} → {internal_ip}:{internal_port}"
        )
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
