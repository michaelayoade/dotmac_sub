"""Admin web routes for OLT inventory, detail, and sync flows."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import cast
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import OLTDevice
from app.services import web_admin as web_admin_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services.audit_helpers import build_audit_activities, log_audit_event
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-olt-inventory"])


def _actor_id(request: Request) -> str | None:
    current_user = web_admin_service.get_current_user(request)
    value = current_user.get("actor_id") or current_user.get("id") if current_user else None
    return str(value) if value else None


def _format_autofind_time(raw: str | None) -> str:
    """Format raw OLT autofind timestamp into a clean display string."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return raw


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


def _authorization_result_query(result: object | None) -> str | None:
    if result is None or not hasattr(result, "to_dict"):
        return None
    try:
        payload = json.dumps(result.to_dict(), separators=(",", ":"))
    except Exception:
        return None
    return quote_plus(payload)


def _authorization_result_from_request(request: Request) -> dict[str, object] | None:
    raw = request.query_params.get("authorize_result")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": message, "type": toast_type}},
            ensure_ascii=True,
        )
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
    "/olts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": None,
            "action_url": "/admin/network/olts",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                db
            ),
            "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(db),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_create(
    request: Request, db: Session = Depends(get_db)
):
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
                "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                    db
                ),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = web_network_olts_service.create_olt_with_audit(
        db, request, values, _actor_id(request)
    )
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": web_network_olts_service.snapshot(values),
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                    db
                ),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    if olt is None:
        raise HTTPException(status_code=500, detail="OLT creation returned no object")
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get(
    "/olts/{olt_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_edit(request: Request, olt_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
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
            "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                db, olt=olt
            ),
            "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(db),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts/{olt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_update(
    request: Request, olt_id: str, db: Session = Depends(get_db)
):
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
                "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                    db, olt=olt
                ),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = web_network_olts_service.update_olt_with_audit(
        db, request, olt_id, olt, values, _actor_id(request)
    )
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": web_network_olts_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt_id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    if olt is None:
        raise HTTPException(status_code=500, detail="OLT update returned no object")
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

    olt_obj = cast(OLTDevice | None, page_data.get("olt"))

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "operations": operations,
            "available_olt_firmware": available_olt_firmware,
            "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                db, olt=olt_obj
            ),
            "ssh_test_status": ssh_test_status,
            "ssh_test_message": ssh_test_message,
            "snmp_test_status": snmp_test_status,
            "snmp_test_message": snmp_test_message,
            "sync_status": sync_status,
            "sync_message": sync_message,
            "authorization_result": _authorization_result_from_request(request),
        }
    )
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.get(
    "/olts/{olt_id}/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_detail_preview(
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
            "Failed to load operation history for OLT preview %s",
            olt_id,
            exc_info=True,
        )
        operations = []
    available_olt_firmware = web_network_olts_service.get_olt_firmware_images(
        db, olt_id
    )

    olt_obj = cast(OLTDevice | None, page_data.get("olt"))

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "operations": operations,
            "available_olt_firmware": available_olt_firmware,
            "operational_acs_server": web_network_olts_service.resolve_operational_acs_server(
                db, olt=olt_obj
            ),
            "ssh_test_status": ssh_test_status,
            "ssh_test_message": ssh_test_message,
            "snmp_test_status": snmp_test_status,
            "snmp_test_message": snmp_test_message,
            "sync_status": sync_status,
            "sync_message": sync_message,
            "authorization_result": _authorization_result_from_request(request),
            "preview_mode": True,
        }
    )
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


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


@router.post(
    "/olts/{olt_id}/vlans/assign",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_assign_vlan(
    request: Request,
    olt_id: str,
    vlan_id: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
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
) -> Response:
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
    ok, message, policy_key = web_network_olts_service.test_olt_ssh_connection(
        db, olt_id
    )
    status = "success" if ok else "error"
    log_audit_event(
        db=db,
        request=request,
        action="test_ssh_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
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
    ok, message = web_network_olts_service.test_olt_snmp_connection(db, olt_id)
    status = "success" if ok else "error"
    log_audit_event(
        db=db,
        request=request,
        action="test_snmp_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
        metadata={"result": "success" if ok else "error", "message": message},
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
    ok, message, capabilities = web_network_olts_service.test_olt_netconf_connection(
        db, olt_id
    )
    status = "success" if ok else "error"
    log_audit_event(
        db=db,
        request=request,
        action="test_netconf_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
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
    current_user = web_admin_service.get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    ok, message, stats = web_network_olts_service.sync_onts_from_olt_snmp_tracked(
        db, olt_id, initiated_by=actor_name
    )
    status = "success" if ok else "error"
    log_audit_event(
        db=db,
        request=request,
        action="sync_onts",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
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
    "/olts/{olt_id}/discover-hardware",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_discover_hardware(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Trigger SNMP Entity MIB hardware discovery for a single OLT."""
    from app.services.network.olt import OLTDevices
    from app.services.network.olt_hardware_discovery import discover_olt_hardware

    try:
        olt = OLTDevices.get(db, olt_id)
    except HTTPException:
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?tab=hardware&sync_status=error&sync_message={quote_plus('OLT not found')}",
            status_code=303,
        )

    ok, message, stats = discover_olt_hardware(db, olt)
    status = "success" if ok else "error"
    log_audit_event(
        db=db,
        request=request,
        action="discover_hardware",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "stats": stats,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=hardware&sync_status={status}&sync_message={quote_plus(message)}",
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
    ok, message, entries = web_network_olts_service.get_autofind_onts(db, olt_id)
    log_audit_event(
        db=db,
        request=request,
        action="autofind_scan",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
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
            "autofind_time": _format_autofind_time(e.autofind_time),
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
    "/olts/{olt_id}/autofind",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_autofind_scan_redirect(olt_id: str) -> RedirectResponse:
    """Redirect accidental GETs back to the OLT autofind tab.

    This primarily covers expired POST flows that were redirected through the
    auth refresh endpoint and then replayed as GET requests by the browser.
    """
    return RedirectResponse(
        url=f"/admin/network/olts/{olt_id}?tab=autofind",
        status_code=303,
    )


@router.post(
    "/unconfigured-onts/scan",
    dependencies=[Depends(require_permission("network:write"))],
)
def unconfigured_onts_scan_now() -> RedirectResponse:
    from app.celery_app import enqueue_celery_task
    from app.tasks.ont_autofind import discover_all_olt_autofind

    enqueue_celery_task(
        discover_all_olt_autofind,
        correlation_id="olt_autofind:all",
        source="admin_network_olts_inventory",
    )
    return RedirectResponse(
        "/admin/network/onts?status=success&message="
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
) -> Response:
    """Authorize a discovered ONT on the OLT via SSH."""
    is_htmx = request.headers.get("HX-Request") == "true"

    if not fsp or not serial_number:
        msg = quote_plus("Missing port or serial number")
        if return_to in ("/admin/network/unconfigured-onts", "/admin/network/onts"):
            return RedirectResponse(f"{return_to}?status=error&message={msg}", status_code=303)
        target = f"/admin/network/olts/{olt_id}?tab=autofind&sync_status=error&sync_message={msg}"
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers("Missing port or serial number", "error"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    from app.services.network.olt_authorization_workflow import (
        authorize_autofind_ont as _authorize_workflow,
    )

    result = _authorize_workflow(db, olt_id, fsp, serial_number)
    status = getattr(result, "status", "success" if result.success else "error")
    completed_authorization = bool(
        getattr(result, "completed_authorization", False)
    )
    log_audit_event(
        db=db,
        request=request,
        action="authorize_ont",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=_actor_id(request),
        metadata={
            "result": status,
            "message": result.message,
            "fsp": fsp,
            "serial_number": serial_number,
        },
        status_code=200 if status in {"success", "warning"} else 500,
        is_success=status == "success",
    )

    result_query = _authorization_result_query(result)

    if completed_authorization and result.ont_unit_id:
        target = (
            f"/admin/network/onts/{result.ont_unit_id}"
            + (f"?authorize_result={result_query}" if result_query else "")
        )
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(result.message, status),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    if return_to in ("/admin/network/unconfigured-onts", "/admin/network/onts"):
        target = (
            f"{return_to}?status={status}&message={quote_plus(result.message)}"
            + (f"&authorize_result={result_query}" if result_query else "")
        )
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(result.message, status),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    target = (
        f"/admin/network/olts/{olt_id}?tab=autofind&sync_status={status}&sync_message={quote_plus(result.message)}"
        + (f"&authorize_result={result_query}" if result_query else "")
    )
    if is_htmx:
        return Response(
            status_code=200,
            headers={
                **_toast_headers(result.message, status),
                "HX-Redirect": target,
            },
        )
    return RedirectResponse(target, status_code=303)


@router.get(
    "/olts/{olt_id}/authorize-ont",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_authorize_ont_redirect(olt_id: str) -> RedirectResponse:
    """Redirect accidental GET replays back to the OLT autofind tab."""
    return olt_autofind_scan_redirect(olt_id)
