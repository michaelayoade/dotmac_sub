"""Admin network CPE management routes."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_cpes as web_network_cpes_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.auth_dependencies import require_permission
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


@router.get("/cpes", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    state = web_network_cpes_service.build_cpe_list_data(
        db,
        search=search,
        status=status,
        vendor=vendor,
        subscriber_id=subscriber_id,
    )
    context = _base_context(request, db, active_page="cpes")
    context.update(state)
    return templates.TemplateResponse("admin/network/cpes/index.html", context)


@router.get("/cpes/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_new(
    request: Request,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": web_network_cpes_service.cpe_form_snapshot({"subscriber_id": subscriber_id or ""}),
            "action_url": "/admin/network/cpes",
            **web_network_cpes_service.cpe_form_reference_data(db, subscriber_id=subscriber_id),
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/form.html", context)


@router.post("/cpes", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def cpe_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_cpes_service.parse_cpe_form(form)
    error = web_network_cpes_service.validate_cpe_values(values)
    if error:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values),
                "action_url": "/admin/network/cpes",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)
    try:
        cpe = web_network_cpes_service.create_cpe(db, values)
    except Exception as exc:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values),
                "action_url": "/admin/network/cpes",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="cpe",
        entity_id=str(cpe.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"serial_number": cpe.serial_number, "vendor": cpe.vendor},
    )
    return RedirectResponse(f"/admin/network/cpes/{cpe.id}", status_code=303)


@router.get("/cpes/{cpe_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_edit(request: Request, cpe_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except (HTTPException, ValueError, LookupError):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    cpe_data = web_network_cpes_service.cpe_form_snapshot_from_model(cpe)
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": cpe_data,
            "action_url": f"/admin/network/cpes/{cpe_id}",
            **web_network_cpes_service.cpe_form_reference_data(
                db, subscriber_id=str(cpe_data.get("subscriber_id") or "")
            ),
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/form.html", context)


@router.post("/cpes/{cpe_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def cpe_update(request: Request, cpe_id: str, db: Session = Depends(get_db)):
    try:
        web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except (HTTPException, ValueError, LookupError):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    form = parse_form_data_sync(request)
    values = web_network_cpes_service.parse_cpe_form(form)
    error = web_network_cpes_service.validate_cpe_values(values)
    if error:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values, cpe_id=cpe_id),
                "action_url": f"/admin/network/cpes/{cpe_id}",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)
    try:
        cpe = web_network_cpes_service.update_cpe(db, cpe_id=cpe_id, values=values)
    except Exception as exc:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": web_network_cpes_service.cpe_form_snapshot(values, cpe_id=cpe_id),
                "action_url": f"/admin/network/cpes/{cpe_id}",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="cpe",
        entity_id=str(cpe.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"serial_number": cpe.serial_number, "vendor": cpe.vendor},
    )
    return RedirectResponse(f"/admin/network/cpes/{cpe.id}", status_code=303)


@router.post("/cpes/{cpe_id}/test-api", dependencies=[Depends(require_permission("network:write"))])
def cpe_test_api(cpe_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except Exception:
        message = quote_plus("CPE not found")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    meta, _ = web_network_cpes_service.parse_cpe_notes_metadata(cpe.notes)
    api_host = str(meta.get("api_host") or "").strip()
    api_port = str(meta.get("api_port") or "8728").strip()
    api_user = str(meta.get("api_user") or "").strip()
    if "mikrotik" not in str(cpe.vendor or "").lower():
        message = quote_plus("API panel is for MikroTik devices")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    if not api_host:
        message = quote_plus("API host is not configured")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    message = f"API configuration looks valid ({api_host}:{api_port}) user={api_user or 'n/a'}"
    encoded_message = quote_plus(message)
    return RedirectResponse(
        f"/admin/network/cpes/{cpe_id}?test_status=success&test_message={encoded_message}",
        status_code=303,
    )


@router.get("/cpes/{cpe_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def cpe_detail(
    request: Request,
    cpe_id: str,
    test_status: str | None = None,
    test_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except (HTTPException, ValueError, LookupError):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    meta, cleaned_notes = web_network_cpes_service.parse_cpe_notes_metadata(cpe.notes)
    activities = build_audit_activities(db, "cpe", str(cpe_id), limit=20)
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": cpe,
            "cpe_meta": meta,
            "cpe_notes": cleaned_notes,
            "is_mikrotik": "mikrotik" in str(cpe.vendor or "").lower(),
            "activities": activities,
            "test_status": test_status,
            "test_message": test_message,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/detail.html", context)


# ── CPE TR-069 Remote Management ─────────────────────────────────


@router.get(
    "/cpes/{cpe_id}/quick-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def cpe_quick_status(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: lightweight TR-069 status for CPE overview sidebar."""
    from app.services.network.cpe_tr069 import CpeTR069

    summary = CpeTR069.get_device_summary(db, cpe_id)
    context = _base_context(request, db, active_page="cpes")
    context["status"] = summary
    return templates.TemplateResponse(
        "admin/network/cpes/_quick_status_partial.html", context
    )


def _cpe_action_response(result: object) -> JSONResponse:
    """Build a JSON response with HX-Trigger toast header from an ActionResult."""
    from app.services.network.ont_action_common import ActionResult

    assert isinstance(result, ActionResult)  # noqa: S101
    status_code = 200 if result.success else 502
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.get(
    "/cpes/{cpe_id}/tr069",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def cpe_tr069_tab(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069 device details for CPE detail page tab."""
    from app.services.network.cpe_tr069 import CpeTR069

    summary = CpeTR069.get_device_summary(db, cpe_id)
    context = _base_context(request, db, active_page="cpes")
    context.update({"tr069": summary, "tr069_available": summary.available})
    return templates.TemplateResponse(
        "admin/network/cpes/_tr069_partial.html", context
    )


@router.post(
    "/cpes/{cpe_id}/reboot",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_reboot(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Reboot CPE device via TR-069."""
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.reboot(db, cpe_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="reboot",
        entity_type="cpe",
        entity_id=cpe_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success, "message": result.message},
    )
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/factory-reset",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_factory_reset(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Factory reset CPE device via TR-069."""
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.factory_reset(db, cpe_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="factory_reset",
        entity_type="cpe",
        entity_id=cpe_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success, "message": result.message},
    )
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/refresh",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_refresh(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Refresh CPE device status from ACS."""
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.refresh_status(db, cpe_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="refresh",
        entity_type="cpe",
        entity_id=cpe_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success},
    )
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/wifi-ssid",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_wifi_ssid(
    request: Request, cpe_id: str, ssid: str = "", db: Session = Depends(get_db)
) -> JSONResponse:
    """Set WiFi SSID on CPE device via TR-069."""
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.set_wifi_ssid(db, cpe_id, ssid)
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/wifi-password",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_wifi_password(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set WiFi password on CPE device via TR-069."""
    form = parse_form_data_sync(request)
    password = str(form.get("password") or "")
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.set_wifi_password(db, cpe_id, password)
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/lan-port",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_lan_port(
    request: Request,
    cpe_id: str,
    port: int = 1,
    enabled: str = "true",
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Toggle LAN port on CPE device via TR-069."""
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.toggle_lan_port(db, cpe_id, port, enabled.lower() == "true")
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/connection-request",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_connection_request(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send connection request to CPE device."""
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.send_connection_request(db, cpe_id)
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/ping-diagnostic",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_ping_diagnostic(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Run ping diagnostic from CPE device via TR-069."""
    form = parse_form_data_sync(request)
    host = str(form.get("host") or "")
    try:
        count = int(str(form.get("count") or 4))
    except (ValueError, TypeError):
        count = 4
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.run_ping_diagnostic(db, cpe_id, host, count)
    return _cpe_action_response(result)


@router.post(
    "/cpes/{cpe_id}/traceroute-diagnostic",
    dependencies=[Depends(require_permission("network:write"))],
)
def cpe_traceroute_diagnostic(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Run traceroute diagnostic from CPE device via TR-069."""
    form = parse_form_data_sync(request)
    host = str(form.get("host") or "8.8.8.8")
    from app.services.network.cpe_actions import CpeActions

    result = CpeActions.run_traceroute_diagnostic(db, cpe_id, host)
    return _cpe_action_response(result)
