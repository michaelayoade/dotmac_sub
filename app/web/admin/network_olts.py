"""Admin network OLT web routes."""

import logging
from datetime import datetime
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_olt_profiles as web_network_olt_profiles_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_ont_autofind as web_network_ont_autofind_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services import (
    web_network_pon_interfaces as web_network_pon_interfaces_service,
)
from app.services.audit_helpers import (
    build_audit_activities,
    log_audit_event,
)
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
    "/olts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olts_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all OLT devices."""
    page_data = web_network_core_devices_service.olts_list_page_data(
        db,
        search=search,
        status=status,
    )
    context = _base_context(request, db, active_page="olts")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/olts/index.html", context)


@router.get(
    "/pon-interfaces",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def pon_interfaces_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    olt_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="pon-interfaces")
    context.update(
        web_network_pon_interfaces_service.build_page_data(
            db,
            search=search,
            status=status,
            olt_id=olt_id,
        )
    )
    return templates.TemplateResponse(
        "admin/network/pon_interfaces/index.html", context
    )


@router.post(
    "/pon-interfaces/alias",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def pon_interface_save_alias(
    request: Request,
    olt_id: str = Form(""),
    interface_name: str = Form(""),
    alias: str = Form(""),
    pon_port_id: str = Form(""),
    return_to: str = Form("/admin/network/pon-interfaces"),
    db: Session = Depends(get_db),
):
    web_network_pon_interfaces_service.save_alias(
        db,
        olt_id=olt_id,
        interface_name=interface_name,
        alias=alias,
        pon_port_id=pon_port_id or None,
    )
    target = return_to or "/admin/network/pon-interfaces"
    return RedirectResponse(url=target, status_code=303)


@router.get(
    "/olts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": None,
            "action_url": "/admin/network/olts",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_create(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    values = web_network_olts_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_olts_service.validate_values(db, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": None,
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    olt, error = web_network_olts_service.create_olt_with_audit(
        db, request, values, actor_id
    )
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": web_network_olts_service.snapshot(values),
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get(
    "/olts/{olt_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_edit(request: Request, olt_id: str, db: Session = Depends(get_db)):
    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": web_network_olts_service.build_form_model(db, olt),
            "action_url": f"/admin/network/olts/{olt.id}",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts/{olt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_update(request: Request, olt_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    values = web_network_olts_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_olts_service.validate_values(db, values, current_olt=olt)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": web_network_olts_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt.id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    olt, error = web_network_olts_service.update_olt_with_audit(
        db, request, olt_id, olt, values, actor_id
    )
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": web_network_olts_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt_id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get(
    "/olts/{olt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_detail(
    request: Request,
    olt_id: str,
    ssh_test_status: str | None = None,
    ssh_test_message: str | None = None,
    snmp_test_status: str | None = None,
    snmp_test_message: str | None = None,
    sync_status: str | None = None,
    sync_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_network_core_devices_service.olt_detail_page_data(db, olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    activities = build_audit_activities(db, "olt", str(olt_id))
    try:
        operations = web_network_operations_service.build_operation_history(
            db, "olt", str(olt_id)
        )
    except Exception:
        logger.error(
            "Failed to load operation history for OLT %s", olt_id, exc_info=True
        )
        operations = []
    available_olt_firmware = web_network_olts_service.get_olt_firmware_images(
        db, olt_id
    )

    # ACS prefill for the TR-069 create modal
    olt_obj = page_data.get("olt")
    acs_prefill: dict[str, str] = {}
    if olt_obj and getattr(olt_obj, "tr069_acs_server", None):
        acs = olt_obj.tr069_acs_server
        acs_prefill = {
            "cwmp_url": acs.cwmp_url or "",
            "cwmp_username": acs.cwmp_username or "",
        }

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "operations": operations,
            "available_olt_firmware": available_olt_firmware,
            "acs_prefill": acs_prefill,
            "ssh_test_status": ssh_test_status,
            "ssh_test_message": ssh_test_message,
            "snmp_test_status": snmp_test_status,
            "snmp_test_message": snmp_test_message,
            "sync_status": sync_status,
            "sync_message": sync_message,
        }
    )
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.post(
    "/olts/{olt_id}/vlans/assign",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_assign_vlan(
    request: Request,
    olt_id: str,
    vlan_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_network_olts_service.assign_vlan_to_olt(db, olt_id, vlan_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=config", status_code=303)


@router.post(
    "/olts/{olt_id}/vlans/unassign",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_unassign_vlan(
    request: Request,
    olt_id: str,
    vlan_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_network_olts_service.unassign_vlan_from_olt(db, olt_id, vlan_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=config", status_code=303)


@router.post(
    "/olts/{olt_id}/ip-pools/assign",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_assign_ip_pool(
    request: Request,
    olt_id: str,
    pool_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_network_olts_service.assign_ip_pool_to_olt(db, olt_id, pool_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=config", status_code=303)


@router.post(
    "/olts/{olt_id}/ip-pools/unassign",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_unassign_ip_pool(
    request: Request,
    olt_id: str,
    pool_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_network_olts_service.unassign_ip_pool_from_olt(db, olt_id, pool_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=config", status_code=303)


@router.post(
    "/olts/{olt_id}/cli",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_run_cli_command(
    request: Request,
    olt_id: str,
    command: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute a read-only CLI command on an OLT and return the output as HTML partial."""
    cmd = command.strip()
    if not cmd:
        return HTMLResponse(
            '<pre class="text-sm text-slate-400 dark:text-slate-500">Enter a command above.</pre>'
        )
    import html as html_mod

    ok, message, output = web_network_olts_service.execute_cli_command(db, olt_id, cmd)
    if not ok:
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 '
            f'dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{html_mod.escape(message)}</div>'
        )
    escaped_output = html_mod.escape(output)
    return HTMLResponse(
        f'<pre class="whitespace-pre-wrap break-words text-sm font-mono text-emerald-800 '
        f'dark:text-emerald-300 bg-slate-900 dark:bg-slate-950 rounded-lg p-4 overflow-x-auto">'
        f"{escaped_output or '(no output)'}</pre>"
    )


@router.post(
    "/olts/{olt_id}/test-ssh",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_test_ssh_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    from app.web.admin import get_current_user

    ok, message, policy_key = web_network_olts_service.test_olt_ssh_connection(
        db, olt_id
    )
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="test_ssh_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "policy_key": policy_key,
            "message": message,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?ssh_test_status={status}&ssh_test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/test-snmp",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_test_snmp_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    from app.web.admin import get_current_user

    ok, message = web_network_olts_service.test_olt_snmp_connection(db, olt_id)
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="test_snmp_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?snmp_test_status={status}&snmp_test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/test-netconf",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_test_netconf_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    from app.web.admin import get_current_user

    ok, message, capabilities = web_network_olts_service.test_olt_netconf_connection(
        db, olt_id
    )
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="test_netconf_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "capabilities_count": len(capabilities),
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?ssh_test_status={status}&ssh_test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/netconf-get-config",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_netconf_get_config(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch OLT running config via NETCONF and return as formatted HTML."""
    import html as html_mod

    ok, message, config_xml = web_network_olts_service.get_olt_netconf_config(
        db, olt_id
    )
    escaped_msg = html_mod.escape(message)
    if not ok:
        return HTMLResponse(
            f'<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            f'dark:border-red-900/30 dark:bg-red-900/10 dark:text-red-300">{escaped_msg}</div>'
        )

    escaped_xml = html_mod.escape(config_xml)
    return HTMLResponse(
        f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700 '
        f'dark:border-emerald-900/30 dark:bg-emerald-900/10 dark:text-emerald-300 mb-3">{escaped_msg}</div>'
        f'<pre class="rounded-lg bg-slate-900 p-4 text-xs font-mono text-emerald-400 overflow-x-auto '
        f'max-h-[600px] overflow-y-auto whitespace-pre-wrap">{escaped_xml}</pre>'
    )


@router.post(
    "/olts/{olt_id}/sync-onts",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_sync_onts(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    ok, message, stats = web_network_olts_service.sync_onts_from_olt_snmp_tracked(
        db, olt_id, initiated_by=actor_name
    )
    status = "success" if ok else "error"
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="sync_onts",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "stats": stats,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}/sync-onts",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_sync_onts_get_fallback(olt_id: str) -> RedirectResponse:
    """GET fallback for auth-refresh redirects targeting the sync POST endpoint."""
    message = quote_plus("Sync ONTs uses POST. Please click Sync ONTs again.")
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status=info&sync_message={message}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/autofind",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_autofind_scan(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Scan OLT for unregistered ONTs via SSH autofind."""
    from app.web.admin import get_current_user

    ok, message, entries = web_network_olts_service.get_autofind_onts(db, olt_id)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="autofind_scan",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "count": len(entries),
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    autofind_data = [
        {
            "fsp": e.fsp,
            "serial_number": e.serial_number,
            "serial_hex": e.serial_hex,
            "vendor_id": e.vendor_id,
            "model": e.model,
            "software_version": e.software_version,
            "mac": e.mac,
            "equipment_sn": e.equipment_sn,
            "autofind_time": e.autofind_time,
        }
        for e in entries
    ]
    return templates.TemplateResponse(
        "admin/network/olts/_autofind_results.html",
        {
            "request": request,
            "olt_id": olt_id,
            "autofind_ok": ok,
            "autofind_message": message,
            "autofind_entries": autofind_data,
        },
    )


@router.get(
    "/unconfigured-onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def unconfigured_onts_list(
    request: Request,
    search: str | None = None,
    olt_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="unconfigured-onts")
    context.update(
        web_network_ont_autofind_service.build_unconfigured_onts_page_data(
            db,
            search=search,
            olt_id=olt_id,
        )
    )
    context["status"] = status
    context["message"] = message
    return templates.TemplateResponse(
        "admin/network/onts/unconfigured_index.html",
        context,
    )


@router.post(
    "/unconfigured-onts/scan",
    dependencies=[Depends(require_permission("network:write"))],
)
def unconfigured_onts_scan_now() -> RedirectResponse:
    from app.tasks.ont_autofind import discover_all_olt_autofind

    discover_all_olt_autofind.delay()
    return RedirectResponse(
        "/admin/network/unconfigured-onts?status=success&message="
        + quote_plus("Aggregated OLT autofind scan queued."),
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/authorize-ont",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_authorize_ont(
    request: Request,
    olt_id: str,
    fsp: str = Form(""),
    serial_number: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Authorize a discovered ONT on the OLT via SSH."""
    from app.web.admin import get_current_user

    if not fsp or not serial_number:
        msg = quote_plus("Missing port or serial number")
        if return_to == "/admin/network/unconfigured-onts":
            return RedirectResponse(
                f"{return_to}?status=error&message={msg}",
                status_code=303,
            )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={msg}",
            status_code=303,
        )

    ok, message = web_network_olts_service.authorize_autofind_ont(
        db, olt_id, fsp, serial_number
    )
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="authorize_ont",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": status,
            "message": message,
            "fsp": fsp,
            "serial_number": serial_number,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )

    # Trigger SNMP discovery to pick up the newly authorized ONT
    if ok:
        try:
            web_network_olts_service.sync_onts_from_olt_snmp(db, olt_id)
        except Exception as e:
            logger.warning("Post-authorize SNMP sync failed for OLT %s: %s", olt_id, e)
        try:
            web_network_ont_autofind_service.resolve_candidate_authorized(
                db,
                olt_id=olt_id,
                fsp=fsp,
                serial_number=serial_number,
            )
        except Exception as e:
            logger.warning(
                "Failed to resolve cached autofind candidate for %s on %s %s: %s",
                serial_number,
                olt_id,
                fsp,
                e,
            )

    if return_to == "/admin/network/unconfigured-onts":
        return RedirectResponse(
            f"{return_to}?status={status}&message={quote_plus(message)}",
            status_code=303,
        )

    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# TR-069 ACS profile management
# ---------------------------------------------------------------------------


@router.post(
    "/olts/{olt_id}/tr069-profiles",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_tr069_profiles_ssh(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Read TR-069 server profiles from OLT via SSH and return partial."""
    ok, message, profiles, extra = web_network_olts_service.get_tr069_profiles_context(
        db, olt_id
    )
    return templates.TemplateResponse(
        "admin/network/olts/_tr069_profiles.html",
        {
            "request": request,
            "olt_id": olt_id,
            "tr069_ok": ok,
            "tr069_message": message,
            "tr069_profiles": profiles,
            "tr069_onts": extra.get("onts", []),
            "acs_prefill": extra.get("acs_prefill", {}),
        },
    )


@router.post(
    "/olts/{olt_id}/tr069-profiles/create",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_tr069_profile_create(
    request: Request,
    olt_id: str,
    profile_name: str = Form(""),
    acs_url: str = Form(""),
    acs_username: str = Form(""),
    acs_password: str = Form(""),
    inform_interval: int = Form(300),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create a TR-069 server profile on the OLT via SSH."""
    from app.web.admin import get_current_user

    ok, message = web_network_olts_service.handle_create_tr069_profile(
        db,
        olt_id,
        profile_name=profile_name.strip(),
        acs_url=acs_url.strip(),
        username=acs_username.strip(),
        password=acs_password.strip(),
        inform_interval=inform_interval,
    )
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="create_tr069_profile",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={"result": "success" if ok else "error", "profile_name": profile_name},
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return JSONResponse(
        {"ok": ok, "message": message},
        status_code=200 if ok else 400,
    )


@router.post(
    "/olts/{olt_id}/tr069-profiles/rebind",
    dependencies=[Depends(require_permission("network:write"))],
)
async def olt_tr069_rebind(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Rebind selected ONTs to a TR-069 server profile."""
    from app.web.admin import get_current_user

    form = await request.form()
    target_profile_id = int(form.get("target_profile_id", 0))
    ont_ids = form.getlist("ont_ids")
    if not ont_ids or not target_profile_id:
        return JSONResponse(
            {"ok": False, "message": "Missing ONT selection or target profile"},
            status_code=400,
        )

    stats = web_network_olts_service.handle_rebind_tr069_profiles(
        db, olt_id, list(ont_ids), target_profile_id
    )
    rebound = stats.get("rebound", 0)
    failed = stats.get("failed", 0)
    errors = stats.get("errors", [])

    message = f"Rebound {rebound} ONT(s) to profile {target_profile_id}"
    if failed:
        message += f", {failed} failed"

    ok = int(rebound) > 0

    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="rebind_tr069_profiles",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "rebound": rebound,
            "failed": failed,
            "target_profile_id": target_profile_id,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return JSONResponse(
        {
            "ok": ok,
            "message": message,
            "rebound": rebound,
            "failed": failed,
            "errors": errors,
        },
        status_code=200 if ok else 400,
    )


@router.post(
    "/olts/{olt_id}/init-tr069",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_init_tr069(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create or verify DotMac-ACS TR-069 profile on the OLT."""
    from app.models.network import OLTDevice as OLTModel

    olt = db.get(OLTModel, olt_id)
    if not olt:
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?error=OLT+not+found", status_code=303
        )

    # Check if profile already exists
    from app.services.network.olt_ssh import (
        create_tr069_server_profile,
        get_tr069_server_profiles,
    )

    ok, msg, profiles = get_tr069_server_profiles(olt)
    if not ok:
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?error={quote_plus(msg)}", status_code=303
        )

    for p in profiles:
        if "dotmac" in p.name.lower() or "10.10.41.1" in (p.acs_url or ""):
            return RedirectResponse(
                f"/admin/network/olts/{olt_id}?notice={quote_plus(f'TR-069 profile already exists: {p.name} (ID {p.profile_id})')}",
                status_code=303,
            )

    # Create profile
    ok, msg = create_tr069_server_profile(
        olt,
        profile_name="DotMac-ACS",
        acs_url="http://10.10.41.1:7547",
        username="acs",
        password="acs",  # nosec  # noqa: S106
        inform_interval=300,
    )

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="init_tr069",
        entity_type="olt",
        entity_id=olt_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": ok, "message": msg},
    )

    status = "notice" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?{status}={quote_plus(msg)}", status_code=303
    )


@router.post(
    "/olts/{olt_id}/firmware-upgrade",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_firmware_upgrade(
    request: Request,
    olt_id: str,
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Trigger firmware upgrade on OLT via SSH."""
    from app.web.admin import get_current_user

    if not firmware_image_id:
        msg = quote_plus("No firmware image selected")
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={msg}",
            status_code=303,
        )

    ok, message = web_network_olts_service.trigger_olt_firmware_upgrade(
        db, olt_id, firmware_image_id
    )
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="firmware_upgrade",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": status,
            "message": message,
            "firmware_image_id": firmware_image_id,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}/backups",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backups_list(
    request: Request,
    olt_id: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    test_status: str | None = None,
    test_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    backups = web_network_olts_service.list_olt_backups(
        db,
        olt_id=olt_id,
        start_at=start_at,
        end_at=end_at,
    )
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": olt,
            "backups": backups,
            "start_at": start_at,
            "end_at": end_at,
            "test_status": test_status,
            "test_message": test_message,
        }
    )
    return templates.TemplateResponse("admin/network/olts/backups.html", context)


@router.get(
    "/olts/backups/{backup_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backup_detail(
    request: Request, backup_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    backup = web_network_olts_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Backup not found"},
            status_code=404,
        )
    olt = web_network_olts_service.get_olt_or_none(db, str(backup.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    preview = web_network_olts_service.read_backup_preview(backup)
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": olt,
            "backup": backup,
            "preview": preview,
        }
    )
    return templates.TemplateResponse("admin/network/olts/backup_detail.html", context)


@router.get(
    "/olts/backups/{backup_id}/download",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backup_download(backup_id: str, db: Session = Depends(get_db)) -> FileResponse:
    backup = web_network_olts_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")
    path = web_network_olts_service.backup_file_path(backup)
    filename = path.name
    return FileResponse(path=path, filename=filename, media_type="text/plain")


@router.post(
    "/olts/{olt_id}/backups/test-connection",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_test_connection(
    olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message = web_network_olts_service.test_olt_connection(db, olt_id)
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/backups/test-backup",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_test_backup(
    olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    backup, message = web_network_olts_service.run_test_backup(db, olt_id)
    if backup is not None:
        status = "success"
    else:
        status = "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/backups/ssh-backup",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_ssh(olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    """Fetch full running config via SSH and save as backup."""
    backup, message = web_network_olts_service.backup_running_config_ssh(db, olt_id)
    status = "success" if backup is not None else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/backups/compare",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_backup_compare(
    request: Request,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        backup1, backup2, diff = web_network_olts_service.compare_olt_backups(
            db, backup_id_1, backup_id_2
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    olt = web_network_olts_service.get_olt_or_none(db, str(backup1.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context.update({"olt": olt, "backup1": backup1, "backup2": backup2, "diff": diff})
    return templates.TemplateResponse("admin/network/olts/backup_compare.html", context)


# ---------------------------------------------------------------------------
# OLT profile display and events
# ---------------------------------------------------------------------------


@router.get(
    "/olts/{olt_id}/events",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_device_events(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: ONT device events (online/offline/signal) for this OLT."""
    data = web_network_olts_service.olt_device_events_context(db, olt_id)
    context = _base_context(request, db, active_page="olts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/olts/_events_partial.html", context
    )


@router.get(
    "/olts/{olt_id}/profiles/line",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_line_profiles(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: OLT line and service profiles."""
    data = web_network_olt_profiles_service.line_profiles_context(db, olt_id)
    context = _base_context(request, db, active_page="olts")
    context.update(data)
    return templates.TemplateResponse("admin/network/olts/_profiles_tab.html", context)


@router.get(
    "/olts/{olt_id}/profiles/tr069",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_tr069_profiles(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: OLT TR-069 server profiles."""
    data = web_network_olt_profiles_service.tr069_profiles_context(db, olt_id)
    context = _base_context(request, db, active_page="olts")
    context.update(data)
    return templates.TemplateResponse("admin/network/olts/_profiles_tab.html", context)
