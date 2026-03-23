"""Admin network OLT/ONT web routes."""

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models.network import (
    ConfigMethod,
    GponChannel,
    IpProtocol,
    MgmtIpMode,
    OnuMode,
    WanMode,
)
from app.services import network as network_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_olt_profiles as web_network_olt_profiles_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services import web_network_ont_autofind as web_network_ont_autofind_service
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services import (
    web_network_pon_interfaces as web_network_pon_interfaces_service,
)
from app.services import web_network_service_ports as web_network_service_ports_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.services.credential_crypto import encrypt_credential
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


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


def _service_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    context = web_network_service_ports_service.list_context(db, ont_id)
    response = templates.TemplateResponse(
        "admin/network/onts/_service_ports_tab.html",
        {"request": request, **context},
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}}
        )
    return response


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    """Build latin-1-safe HX-Trigger headers for toast notifications."""
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _ont_form_dependencies(db: Session, ont: Any | None = None) -> dict:
    """Build all dropdown data needed by the ONT provisioning form."""
    deps = web_network_onts_service.ont_form_dependencies(db, ont)
    deps["gpon_channels"] = [e.value for e in GponChannel]
    deps["onu_modes"] = [e.value for e in OnuMode]
    return deps


def _ont_has_active_assignment(db: Session, ont_id: str) -> bool:
    """Return True when the ONT currently has an active assignment."""
    return web_network_ont_assignments_service.has_active_assignment(db, ont_id)


def _form_uuid_or_none(form: FormData, key: str) -> str | None:
    """Extract a UUID string from form data, returning None if empty."""
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    return raw.strip() or None


def _form_float_or_none(form: FormData, key: str) -> float | None:
    """Extract a float from form data, returning None if empty or invalid."""
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


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


@router.get(
    "/onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def onts_list(
    request: Request,
    view: str = "list",
    status: str | None = None,
    olt_id: str | None = None,
    pon_port_id: str | None = None,
    pon_hint: str | None = None,
    zone_id: str | None = None,
    online_status: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    order_by: str = "serial_number",
    order_dir: str = "asc",
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all ONT/CPE devices with advanced filtering."""
    page_data = web_network_core_devices_service.onts_list_page_data(
        db,
        view=view,
        status=status,
        olt_id=olt_id,
        pon_port_id=pon_port_id,
        pon_hint=pon_hint,
        zone_id=zone_id,
        online_status=online_status,
        signal_quality=signal_quality,
        search=search,
        vendor=vendor,
        order_by=order_by,
        order_dir=order_dir,
        page=page,
    )
    context = _base_context(request, db, active_page="onts")
    context.update(page_data)
    context["firmware_images"] = web_network_onts_service.get_active_firmware_images(db)
    return templates.TemplateResponse("admin/network/onts/index.html", context)


@router.post(
    "/onts/bulk-action",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def onts_bulk_action(
    request: Request,
    action: str = Form(""),
    ont_ids: list[str] = Form(default=[]),
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute a bulk action on selected ONTs."""
    stats = web_network_onts_service.execute_bulk_action(
        db, ont_ids, action, firmware_image_id=firmware_image_id or None
    )
    error = stats.get("error")
    if error:
        summary = f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{error}</div>'
    else:
        skipped_text = (
            f", {stats.get('skipped', 0)} skipped (max 50)"
            if stats.get("skipped")
            else ""
        )
        summary = (
            f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 '
            f'dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-400">'
            f"Bulk <strong>{action}</strong>: {stats['succeeded']} succeeded, {stats['failed']} failed"
            f"{skipped_text}."
            f"</div>"
        )
    return HTMLResponse(summary)


@router.get(
    "/onts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": None,
            "action_url": "/admin/network/onts",
            **_ont_form_dependencies(db),
        }
    )
    return templates.TemplateResponse("admin/network/onts/form.html", context)


def _ont_unit_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_ont_units_serial_number" in message:
        return "Serial number already exists"
    return "ONT could not be saved due to a data conflict"


@router.post(
    "/onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_create(request: Request, db: Session = Depends(get_db)):
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from app.schemas.network import OntUnitCreate

    form = parse_form_data_sync(request)
    serial_number = _form_str(form, "serial_number").strip()

    if not serial_number:
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": None,
                "action_url": "/admin/network/onts",
                "error": "Serial number is required",
                **_ont_form_dependencies(db),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitCreate(
        serial_number=serial_number,
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
        # Imported / external provisioning fields
        onu_type_id=_form_uuid_or_none(form, "onu_type_id"),
        olt_device_id=_form_uuid_or_none(form, "olt_device_id"),
        pon_type=_form_str(form, "pon_type").strip() or None,
        gpon_channel=_form_str(form, "gpon_channel").strip() or None,
        board=_form_str(form, "board").strip() or None,
        port=_form_str(form, "port").strip() or None,
        onu_mode=_form_str(form, "onu_mode").strip() or None,
        user_vlan_id=_form_uuid_or_none(form, "user_vlan_id"),
        zone_id=_form_uuid_or_none(form, "zone_id"),
        splitter_id=_form_uuid_or_none(form, "splitter_id"),
        splitter_port_id=_form_uuid_or_none(form, "splitter_port_id"),
        download_speed_profile_id=_form_uuid_or_none(form, "download_speed_profile_id"),
        upload_speed_profile_id=_form_uuid_or_none(form, "upload_speed_profile_id"),
        name=_form_str(form, "name").strip() or None,
        address_or_comment=_form_str(form, "address_or_comment").strip() or None,
        external_id=_form_str(form, "external_id").strip() or None,
        use_gps=_form_str(form, "use_gps") == "true",
        gps_latitude=_form_float_or_none(form, "gps_latitude"),
        gps_longitude=_form_float_or_none(form, "gps_longitude"),
    )

    if payload.is_active:
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": payload,
                "action_url": "/admin/network/onts",
                "error": "New ONTs must be inactive until assigned to a customer.",
                **_ont_form_dependencies(db, payload),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    try:
        ont = network_service.ont_units.create(db=db, payload=payload)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ont",
            entity_id=str(ont.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"serial_number": ont.serial_number},
        )
    except IntegrityError as exc:
        db.rollback()
        error = _ont_unit_integrity_error_message(exc)
        ont_snapshot = SimpleNamespace(**payload.model_dump())
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": ont_snapshot,
                "action_url": "/admin/network/onts",
                "error": error,
                **_ont_form_dependencies(db, payload),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.get(
    "/onts/{ont_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_edit(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": ont,
            "action_url": f"/admin/network/onts/{ont.id}",
            **_ont_form_dependencies(db, ont),
        }
    )
    return templates.TemplateResponse("admin/network/onts/form.html", context)


@router.get(
    "/onts/{ont_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_detail(
    request: Request,
    ont_id: str,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_network_core_devices_service.ont_detail_page_data(db, ont_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    allowed_tabs = {
        "overview",
        "network",
        "history",
        "tr069",
        "charts",
        "service-ports",
        "provisioning",
    }
    active_tab = tab if tab in allowed_tabs else "overview"

    activities = build_audit_activities(db, "ont", str(ont_id))
    try:
        operations = web_network_operations_service.build_operation_history(
            db, "ont", str(ont_id)
        )
    except Exception:
        logger.error(
            "Failed to load operation history for ONT %s", ont_id, exc_info=True
        )
        operations = []
    profiles = web_network_onts_service.get_provisioning_profiles(db)

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
            "profiles": profiles,
        }
    )
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get(
    "/onts/{ont_id}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_assign_new(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    deps = web_network_ont_assignments_service.assignment_form_dependencies(db)
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": ont,
            **deps,
            "action_url": f"/admin/network/onts/{ont.id}/assign",
        }
    )
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.post(
    "/onts/{ont_id}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_assign_create(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError

    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    values = web_network_ont_assignments_service.parse_form_values(
        parse_form_data_sync(request)
    )
    error = web_network_ont_assignments_service.validate_form_values(values)
    if not error and web_network_ont_assignments_service.has_active_assignment(
        db, ont_id
    ):
        error = "This ONT is already assigned"

    if error:
        deps = web_network_ont_assignments_service.assignment_form_dependencies(db)
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": ont,
                **deps,
                "action_url": f"/admin/network/onts/{ont.id}/assign",
                "error": error,
                "form": web_network_ont_assignments_service.form_payload(values),
            }
        )
        return templates.TemplateResponse("admin/network/onts/assign.html", context)
    try:
        web_network_ont_assignments_service.create_assignment(db, ont, values)
    except IntegrityError as exc:
        db.rollback()
        msg = (
            "This ONT is already assigned. Refresh the page and try again."
            if "ix_ont_assignments_active_unit" in str(exc)
            else "Could not create assignment due to a data conflict."
        )
        deps = web_network_ont_assignments_service.assignment_form_dependencies(db)
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": ont,
                **deps,
                "action_url": f"/admin/network/onts/{ont.id}/assign",
                "error": msg,
                "form": web_network_ont_assignments_service.form_payload(values),
            }
        )
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.post(
    "/onts/{ont_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_update(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from app.schemas.network import OntUnitUpdate

    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    serial_number = _form_str(form, "serial_number").strip()

    if not serial_number:
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": ont,
                "action_url": f"/admin/network/onts/{ont.id}",
                "error": "Serial number is required",
                **_ont_form_dependencies(db, ont),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitUpdate(
        serial_number=serial_number,
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
        # Imported / external provisioning fields
        onu_type_id=_form_uuid_or_none(form, "onu_type_id"),
        olt_device_id=_form_uuid_or_none(form, "olt_device_id"),
        pon_type=_form_str(form, "pon_type").strip() or None,
        gpon_channel=_form_str(form, "gpon_channel").strip() or None,
        board=_form_str(form, "board").strip() or None,
        port=_form_str(form, "port").strip() or None,
        onu_mode=_form_str(form, "onu_mode").strip() or None,
        user_vlan_id=_form_uuid_or_none(form, "user_vlan_id"),
        zone_id=_form_uuid_or_none(form, "zone_id"),
        splitter_id=_form_uuid_or_none(form, "splitter_id"),
        splitter_port_id=_form_uuid_or_none(form, "splitter_port_id"),
        download_speed_profile_id=_form_uuid_or_none(form, "download_speed_profile_id"),
        upload_speed_profile_id=_form_uuid_or_none(form, "upload_speed_profile_id"),
        name=_form_str(form, "name").strip() or None,
        address_or_comment=_form_str(form, "address_or_comment").strip() or None,
        external_id=_form_str(form, "external_id").strip() or None,
        use_gps=_form_str(form, "use_gps") == "true",
        gps_latitude=_form_float_or_none(form, "gps_latitude"),
        gps_longitude=_form_float_or_none(form, "gps_longitude"),
    )

    if payload.is_active and not _ont_has_active_assignment(db, ont_id):
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": payload,
                "action_url": f"/admin/network/onts/{ont.id}",
                "error": "ONT cannot be active until it has an active assignment.",
                **_ont_form_dependencies(db, payload),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    try:
        before_snapshot = model_to_dict(ont)
        ont = network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
        after = network_service.ont_units.get_including_inactive(
            db=db, entity_id=ont_id
        )
        after_snapshot = model_to_dict(after)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata_payload = {"changes": changes} if changes else None
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="ont",
            entity_id=str(ont_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except IntegrityError as exc:
        db.rollback()
        error = _ont_unit_integrity_error_message(exc)
        ont_snapshot = SimpleNamespace(**payload.model_dump())
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": ont_snapshot,
                "action_url": f"/admin/network/onts/{ont_id}",
                "error": error,
                **_ont_form_dependencies(db, payload),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


# ── ONU Mode / Mgmt IP Modals ──────────────────────────────────────


@router.get(
    "/onts/{ont_id}/onu-mode",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_onu_mode_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve ONU mode configuration modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    context = {
        "request": request,
        "ont": ont,
        "vlans": vlans,
        "wan_modes": [e.value for e in WanMode],
        "config_methods": [e.value for e in ConfigMethod],
        "ip_protocols": [e.value for e in IpProtocol],
        "onu_modes": [e.value for e in OnuMode],
    }
    return templates.TemplateResponse(
        "admin/network/onts/_onu_mode_modal.html", context
    )


@router.post(
    "/onts/{ont_id}/onu-mode",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_onu_mode_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update ONU mode configuration."""
    from app.schemas.network import OntUnitUpdate

    form = parse_form_data_sync(request)
    payload = OntUnitUpdate(
        onu_mode=_form_str(form, "onu_mode").strip() or None,
        wan_vlan_id=_form_uuid_or_none(form, "wan_vlan_id"),
        wan_mode=_form_str(form, "wan_mode").strip() or None,
        config_method=_form_str(form, "config_method").strip() or None,
        ip_protocol=_form_str(form, "ip_protocol").strip() or None,
        pppoe_username=_form_str(form, "pppoe_username").strip() or None,
        pppoe_password=encrypt_credential(pw)
        if (pw := _form_str(form, "pppoe_password").strip())
        else None,
        wan_remote_access=_form_str(form, "wan_remote_access") == "true",
    )

    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    after_snapshot = model_to_dict(after)
    changes = diff_dicts(before_snapshot, after_snapshot)

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update_onu_mode",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"changes": changes} if changes else None,
    )
    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/mgmt-ip",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_mgmt_ip_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve management/VoIP IP modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    context = {
        "request": request,
        "ont": ont,
        "vlans": vlans,
        "mgmt_ip_modes": [e.value for e in MgmtIpMode],
    }
    return templates.TemplateResponse("admin/network/onts/_mgmt_ip_modal.html", context)


@router.post(
    "/onts/{ont_id}/mgmt-ip",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_mgmt_ip_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update management/VoIP IP configuration."""
    from app.schemas.network import OntUnitUpdate

    form = parse_form_data_sync(request)
    payload = OntUnitUpdate(
        mgmt_ip_mode=_form_str(form, "mgmt_ip_mode").strip() or None,
        mgmt_vlan_id=_form_uuid_or_none(form, "mgmt_vlan_id"),
        mgmt_ip_address=_form_str(form, "mgmt_ip_address").strip() or None,
        mgmt_remote_access=_form_str(form, "mgmt_remote_access") == "true",
        voip_enabled=_form_str(form, "voip_enabled") == "true",
    )

    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    after_snapshot = model_to_dict(after)
    changes = diff_dicts(before_snapshot, after_snapshot)

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update_mgmt_ip",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"changes": changes} if changes else None,
    )
    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


# ── ONT Remote Actions ─────────────────────────────────────────────


@router.post(
    "/onts/{ont_id}/reboot", dependencies=[Depends(require_permission("network:write"))]
)
def ont_reboot(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send reboot command to ONT via GenieACS."""
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    result = web_network_ont_actions_service.execute_reboot(
        db, ont_id, initiated_by=actor_name
    )
    log_audit_event(
        db=db,
        request=request,
        action="reboot",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success, "message": result.message},
    )
    status_code = 200 if result.success else 502
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/refresh",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Force status refresh for ONT via GenieACS."""
    result = web_network_ont_actions_service.execute_refresh(db, ont_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="refresh",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success},
    )
    status_code = 200 if result.success else 502
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.get(
    "/onts/{ont_id}/config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch and display running config from ONT."""
    result = web_network_ont_actions_service.fetch_running_config(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "config_result": result,
        }
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_partial.html", context
    )


@router.post(
    "/onts/{ont_id}/return-to-inventory",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_return_to_inventory(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> Response:
    """Deactivate an ONT and reset it to reusable inventory state."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return JSONResponse(
            {"success": False, "message": "ONT not found"},
            status_code=404,
            headers=_toast_headers("ONT not found", "error"),
        )

    result = web_network_ont_actions_service.return_to_inventory(db, ont_id)
    if result.success:
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="return_to_inventory",
            entity_type="ont",
            entity_id=str(ont.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"serial_number": ont.serial_number},
        )

    return Response(
        status_code=200 if result.success else 400,
        headers={
            **_toast_headers(result.message, "success" if result.success else "error"),
            "HX-Refresh": "true",
        },
    )


@router.post(
    "/onts/{ont_id}/factory-reset",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_factory_reset(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send factory reset command to ONT via GenieACS."""
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    result = web_network_ont_actions_service.execute_factory_reset(
        db, ont_id, initiated_by=actor_name
    )
    log_audit_event(
        db=db,
        request=request,
        action="factory_reset",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success, "message": result.message},
    )
    status_code = 200 if result.success else 502
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/apply-profile",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_apply_profile(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Apply a provisioning profile to an ONT."""
    form = parse_form_data_sync(request)
    profile_id = _form_str(form, "profile_id")
    if not profile_id:
        return JSONResponse(
            {"success": False, "message": "No profile selected"},
            status_code=400,
            headers=_toast_headers("No profile selected", "error"),
        )

    from app.services.network.ont_profile_apply import apply_profile_to_ont

    result = apply_profile_to_ont(db, ont_id, profile_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="apply_profile",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "profile_id": profile_id,
            "success": result.success,
            "fields_updated": result.fields_updated,
        },
    )
    status_code = 200 if result.success else 400
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/firmware-upgrade",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_firmware_upgrade(
    request: Request,
    ont_id: str,
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Trigger firmware upgrade on ONT via TR-069 Download RPC."""
    if not firmware_image_id:
        return JSONResponse(
            {"success": False, "message": "No firmware image selected"},
            status_code=400,
            headers=_toast_headers("No firmware image selected", "error"),
        )

    from app.services.network.ont_actions import OntActions

    result = OntActions.firmware_upgrade(db, ont_id, firmware_image_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="firmware_upgrade",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"firmware_image_id": firmware_image_id, "success": result.success},
    )
    status_code = 200 if result.success else 400
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/wifi-ssid",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_wifi_ssid(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set WiFi SSID on ONT via GenieACS TR-069."""
    ssid = request.query_params.get("ssid", "")
    result = web_network_ont_actions_service.set_wifi_ssid(db, ont_id, ssid)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="set_wifi_ssid",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success, "ssid": ssid},
    )
    status_code = 200 if result.success else 502
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/wifi-password",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_wifi_password(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
    password: str = Form(""),
) -> JSONResponse:
    """Set WiFi password on ONT via GenieACS TR-069."""
    result = web_network_ont_actions_service.set_wifi_password(db, ont_id, password)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="set_wifi_password",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"success": result.success},
    )
    status_code = 200 if result.success else 502
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/lan-port",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_toggle_lan_port(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Toggle LAN port on ONT via GenieACS TR-069."""
    port_str = request.query_params.get("port", "1")
    enabled_str = request.query_params.get("enabled", "true")
    try:
        port = int(port_str)
    except ValueError:
        port = 1
    enabled = enabled_str.lower() in ("true", "1", "yes")
    result = web_network_ont_actions_service.toggle_lan_port(db, ont_id, port, enabled)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="toggle_lan_port",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "success": result.success,
            "port": port,
            "enabled": enabled,
        },
    )
    status_code = 200 if result.success else 502
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/pppoe-credentials",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_pppoe_credentials(
    request: Request,
    ont_id: str,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Push PPPoE credentials to ONT via TR-069."""
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    result = web_network_ont_actions_service.set_pppoe_credentials(
        db, ont_id, username, password, initiated_by=actor_name
    )
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="set_pppoe_credentials",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if result.success else "error",
            "message": result.message,
            "username": username,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=200 if result.success else 502,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/ping-diagnostic",
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_ping_diagnostic(
    request: Request,
    ont_id: str,
    host: str = Form(""),
    count: int = Form(4),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Run ping diagnostic from ONT via TR-069."""
    from app.web.admin import get_current_user

    result = web_network_ont_actions_service.run_ping_diagnostic(
        db, ont_id, host, count
    )
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="ping_diagnostic",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if result.success else "error",
            "host": host,
            "count": count,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=200 if result.success else 502,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/traceroute-diagnostic",
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_traceroute_diagnostic(
    request: Request,
    ont_id: str,
    host: str = Form(""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Run traceroute diagnostic from ONT via TR-069."""
    from app.web.admin import get_current_user

    result = web_network_ont_actions_service.run_traceroute_diagnostic(db, ont_id, host)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="traceroute_diagnostic",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=actor_id,
        metadata={"result": "success" if result.success else "error", "host": host},
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=200 if result.success else 502,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/enable-ipv6",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_enable_ipv6(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Enable IPv6 dual-stack on an ONT via TR-069."""
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    result = web_network_ont_actions_service.execute_enable_ipv6(
        db, ont_id, initiated_by=actor_name
    )
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=200 if result.success else 502,
        headers=headers,
    )


@router.post(
    "/onts/{ont_id}/connection-request",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_connection_request(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send a TR-069 connection request to an ONT for on-demand management."""
    from app.services.network.ont_action_network import send_connection_request_tracked
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    result = send_connection_request_tracked(db, ont_id, initiated_by=actor_name)
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=200 if result.success else 502,
        headers=headers,
    )


@router.get(
    "/onts/{ont_id}/lan-hosts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_lan_hosts(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: LAN hosts connected to an ONT."""
    from app.services.network.ont_read import ont_read

    lan_hosts = ont_read.get_lan_hosts(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context["lan_hosts"] = lan_hosts
    return templates.TemplateResponse(
        "admin/network/onts/_lan_hosts_partial.html", context
    )


@router.get(
    "/onts/{ont_id}/ethernet-ports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_ethernet_ports(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Ethernet port status for an ONT."""
    from app.services.network.ont_read import ont_read

    ethernet_ports = ont_read.get_ethernet_ports(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context["ethernet_ports"] = ethernet_ports
    return templates.TemplateResponse(
        "admin/network/onts/_ethernet_ports_partial.html", context
    )


@router.get(
    "/onts/{ont_id}/tr069",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_tr069_detail(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069 device details for ONT detail page tab."""
    data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse("admin/network/onts/_tr069_partial.html", context)


@router.get(
    "/onts/{ont_id}/charts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_charts(
    request: Request,
    ont_id: str,
    time_range: str = "24h",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Traffic and signal charts for ONT detail page."""
    data = web_network_ont_charts_service.charts_tab_data(db, ont_id, time_range)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_charts_partial.html", context
    )


# ────────────────────────────────────────────────────────────────────
# Service-port management routes (Phase 1)
# ────────────────────────────────────────────────────────────────────


@router.get(
    "/onts/{ont_id}/service-ports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_service_ports(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Service-ports tab for ONT detail page."""
    data = web_network_service_ports_service.list_context(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_service_ports_tab.html", context
    )


@router.post(
    "/onts/{ont_id}/service-ports/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_service_port_create(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    gem_index: int = Form(default=1),
    user_vlan: str = Form(default=""),
    tag_transform: str = Form(default="translate"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Create a single service-port on the OLT for this ONT."""
    resolved_user_vlan: int | str | None = None
    raw_user_vlan = user_vlan.strip()
    if raw_user_vlan:
        if raw_user_vlan == "untagged":
            resolved_user_vlan = "untagged"
        else:
            try:
                resolved_user_vlan = int(raw_user_vlan)
            except ValueError:
                return _service_ports_partial_response(
                    request,
                    db,
                    ont_id,
                    toast_message="User VLAN must be a number or 'untagged'",
                    toast_type="error",
                )

    ok, msg = web_network_service_ports_service.handle_create(
        db,
        ont_id,
        vlan_id,
        gem_index,
        user_vlan=resolved_user_vlan,
        tag_transform=tag_transform,
    )
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


@router.post(
    "/onts/{ont_id}/service-ports/{index}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_service_port_delete(
    request: Request,
    ont_id: str,
    index: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Delete a service-port from the OLT by index."""
    ok, msg = web_network_service_ports_service.handle_delete(db, ont_id, index)
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


@router.post(
    "/onts/{ont_id}/service-ports/clone",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_service_port_clone(
    request: Request,
    ont_id: str,
    ref_ont_id: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Clone service-ports from a reference ONT."""
    ok, msg = web_network_service_ports_service.handle_clone(db, ont_id, ref_ont_id)
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


# ────────────────────────────────────────────────────────────────────
# ONT management IP / OMCI / TR-069 routes (Phase 2)
# ────────────────────────────────────────────────────────────────────


@router.post(
    "/onts/{ont_id}/actions/omci-reboot",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_omci_reboot(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Reboot ONT via OMCI through the OLT."""
    ok, msg = web_network_ont_actions_service.execute_omci_reboot(db, ont_id)
    return JSONResponse(
        content={"success": ok, "message": msg},
        status_code=200 if ok else 400,
        headers={
            "HX-Trigger": f'{{"showToast": {{"message": "{msg}", "type": "{"success" if ok else "error"}"}}}}'
        },
    )


@router.post(
    "/onts/{ont_id}/actions/configure-mgmt-ip",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_configure_mgmt_ip(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    ip_mode: str = Form(default="dhcp"),
    ip_address: str = Form(default=""),
    subnet: str = Form(default=""),
    gateway: str = Form(default=""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure ONT management IP via OLT IPHOST command."""
    ok, msg = web_network_ont_actions_service.configure_management_ip(
        db,
        ont_id,
        vlan_id,
        ip_mode,
        ip_address=ip_address or None,
        subnet=subnet or None,
        gateway=gateway or None,
    )
    return JSONResponse(
        content={"success": ok, "message": msg},
        status_code=200 if ok else 400,
        headers={
            "HX-Trigger": f'{{"showToast": {{"message": "{msg}", "type": "{"success" if ok else "error"}"}}}}'
        },
    )


@router.post(
    "/onts/{ont_id}/actions/bind-tr069-profile",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_bind_tr069_profile(
    request: Request,
    ont_id: str,
    profile_id: int = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bind TR-069 server profile to ONT via OLT."""
    ok, msg = web_network_ont_actions_service.bind_tr069_profile(db, ont_id, profile_id)
    return JSONResponse(
        content={"success": ok, "message": msg},
        status_code=200 if ok else 400,
        headers={
            "HX-Trigger": f'{{"showToast": {{"message": "{msg}", "type": "{"success" if ok else "error"}"}}}}'
        },
    )


@router.get(
    "/onts/{ont_id}/iphost-config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_iphost_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Management IP config for ONT detail page."""
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    ok, msg, config = web_network_ont_actions_service.fetch_iphost_config(db, ont_id)
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": ont,
            "iphost_config": config,
            "iphost_ok": ok,
            "iphost_msg": msg,
            "vlans": vlans,
            "tr069_profiles": tr069_profiles,
            "tr069_profiles_error": tr069_profiles_error,
        }
    )
    return templates.TemplateResponse("admin/network/onts/_mgmt_config.html", context)


# ────────────────────────────────────────────────────────────────────
# OLT profile display routes (Phase 3)
# ────────────────────────────────────────────────────────────────────


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


@router.get(
    "/onts/{ont_id}/provisioning-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_provisioning_preview(
    request: Request,
    ont_id: str,
    profile_id: str = Query(...),
    tr069_profile_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Command preview for provisioning an ONT."""
    data = web_network_olt_profiles_service.command_preview_context(
        db, ont_id, profile_id, tr069_olt_profile_id=tr069_profile_id
    )
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_provisioning_preview.html", context
    )


# ────────────────────────────────────────────────────────────────────
# End-to-end provisioning routes (Phase 4)
# ────────────────────────────────────────────────────────────────────


@router.get(
    "/onts/{ont_id}/preflight",
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_preflight_check(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
):
    """Pre-flight validation for ONT provisioning. Returns JSON checklist."""
    from fastapi.responses import JSONResponse

    from app.services.network.ont_provisioning_orchestrator import (
        OntProvisioningOrchestrator,
    )

    result = OntProvisioningOrchestrator.validate_prerequisites(db, ont_id)
    return JSONResponse(result)


@router.post(
    "/onts/{ont_id}/provision",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_provision(
    request: Request,
    ont_id: str,
    profile_id: str = Form(...),
    dry_run: bool = Form(default=False),
    tr069_profile_id: int | None = Form(default=None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Trigger end-to-end ONT provisioning (dispatches Celery task)."""
    from app.tasks.provisioning import provision_ont

    task = provision_ont.delay(
        ont_id=ont_id,
        profile_id=profile_id,
        dry_run=dry_run,
        tr069_olt_profile_id=tr069_profile_id,
    )
    return JSONResponse(
        content={
            "success": True,
            "message": "Provisioning task dispatched",
            "task_id": task.id,
        },
        headers={
            "HX-Trigger": '{"showToast": {"message": "Provisioning task started", "type": "success"}}'
        },
    )


@router.get(
    "/onts/{ont_id}/provision-status",
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_provision_status(
    request: Request,
    ont_id: str,
    task_id: str = Query(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Poll provisioning task status."""
    from celery.result import AsyncResult

    result = AsyncResult(task_id)
    if result.ready():
        status = "failed" if result.failed() else "complete"
        result_payload = (
            result.result
            if isinstance(result.result, dict)
            else {
                "success": False,
                "message": str(result.result),
                "steps": [],
            }
        )
        return JSONResponse(
            content={
                "status": status,
                "state": result.state,
                "result": result_payload,
            }
        )
    return JSONResponse(content={"status": "pending", "state": result.state})
