"""Admin web routes for OLT inventory, detail, and sync flows."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import OLTDevice
from app.services import web_admin as web_admin_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_autofind as web_network_ont_autofind_service
from app.services import web_network_onts as web_network_onts_service
from app.services import (
    web_network_pon_interfaces as web_network_pon_interfaces_service,
)
from app.services.auth_dependencies import require_permission
from app.services.ipam_adapter import ipam_adapter
from app.services.network import olt_autofind as olt_autofind_service
from app.services.network import olt_snmp_sync as olt_snmp_sync_service
from app.services.network import olt_tr069_admin as olt_tr069_admin_service
from app.services.network import olt_web_forms as olt_web_forms_service
from app.services.network import olt_web_topology as olt_web_topology_service
from app.services.network.action_logging import actor_label, log_network_action_result
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.ont_scope import can_manage_ont_id, is_internal_operator
from app.services.olt_action_adapter import olt_action_adapter as olt_operations_service
from app.services.olt_detail_adapter import olt_detail_adapter
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-olt-inventory"])


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


def _acs_prefill_from_olt(olt: OLTDevice | None) -> dict[str, str]:
    acs = getattr(olt, "tr069_acs_server", None) if olt else None
    if acs is None:
        return {}
    return {
        "cwmp_url": getattr(acs, "cwmp_url", "") or "",
        "cwmp_username": getattr(acs, "cwmp_username", "") or "",
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


def _log_olt_action_result(
    *,
    request: Request | None,
    olt_id: str | None,
    action: str,
    ok: bool,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    log_network_action_result(
        request=request,
        resource_type="olt",
        resource_id=olt_id,
        action=action,
        success=ok,
        message=message,
        metadata=metadata,
    )


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
    olt_id: str = Form(""),
    interface_name: str = Form(""),
    alias: str = Form(""),
    pon_port_id: str = Form(""),
    return_to: str = Form("/admin/network/pon-interfaces"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_network_pon_interfaces_service.save_alias(
        db,
        olt_id=olt_id,
        interface_name=interface_name,
        alias=alias,
        pon_port_id=pon_port_id or None,
    )
    return RedirectResponse(
        url=return_to or "/admin/network/pon-interfaces",
        status_code=303,
    )


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
            "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                db
            ),
            "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(
                db
            ),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_create(request: Request, db: Session = Depends(get_db)):
    values = olt_web_forms_service.parse_form_values(parse_form_data_sync(request))
    error = olt_web_forms_service.validate_values(db, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": None,
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                    db
                ),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(
                    db
                ),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = olt_web_forms_service.create_olt_with_audit(db, request, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": olt_web_forms_service.snapshot(values),
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                    db
                ),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(
                    db
                ),
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
def olt_edit(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": olt_web_forms_service.build_form_model(db, olt),
            "action_url": f"/admin/network/olts/{olt.id}",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                db, olt=olt
            ),
            "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(
                db
            ),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts/{olt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_update(request: Request, olt_id: str, db: Session = Depends(get_db)):
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    values = olt_web_forms_service.parse_form_values(parse_form_data_sync(request))
    error = olt_web_forms_service.validate_values(db, values, current_olt=olt)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": olt_web_forms_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt.id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                    db, olt=olt
                ),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(
                    db
                ),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = olt_web_forms_service.update_olt_with_audit(
        db, request, olt_id, olt, values
    )
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": olt_web_forms_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt_id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "provisioning_profiles": web_network_onts_service.get_provisioning_profiles(
                    db
                ),
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
    page_data = olt_detail_adapter.page_data(db, olt_id=olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
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
    page_data = olt_detail_adapter.page_data(db, olt_id=olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
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
    data = olt_detail_adapter.events_context(db, olt_id=olt_id)
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
    ipam_adapter.assign_vlan_to_olt(db, olt_id, vlan_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=settings", status_code=303)


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
    ipam_adapter.unassign_vlan_from_olt(db, olt_id, vlan_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=settings", status_code=303)


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
    ipam_adapter.assign_ip_pool_to_olt(db, olt_id, pool_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=settings", status_code=303)


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
    ipam_adapter.unassign_ip_pool_from_olt(db, olt_id, pool_id)
    return RedirectResponse(f"/admin/network/olts/{olt_id}?tab=settings", status_code=303)


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

    ok, message, output = olt_operations_service.execute_cli_command(db, olt_id, cmd)
    if not ok:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Run CLI Command",
            ok=ok,
            message=message,
            metadata={"command": cmd},
        )
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


def _olt_status_by_serial_html(
    *, ok: bool, message: str, status: dict[str, object]
) -> HTMLResponse:
    import html as html_mod

    escaped_msg = html_mod.escape(message)
    if not ok:
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 '
            f'dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{escaped_msg}</div>'
        )

    rows = [
        ("Requested Serial", status.get("requested_serial")),
        ("Lookup Serial", status.get("lookup_serial")),
        ("Registered Serial", status.get("registered_serial")),
        ("Status Serial", status.get("status_serial")),
        ("F/S/P", status.get("fsp")),
        ("ONT-ID", status.get("ont_id")),
        ("Run State", status.get("run_state")),
        ("Config State", status.get("config_state")),
        ("Match State", status.get("match_state")),
    ]
    row_html = "".join(
        "<tr>"
        f'<th class="px-3 py-2 text-left text-xs font-semibold uppercase text-slate-500 dark:text-slate-400">{html_mod.escape(label)}</th>'
        f'<td class="px-3 py-2 font-mono text-sm text-slate-900 dark:text-slate-100">{html_mod.escape(str(value or "-"))}</td>'
        "</tr>"
        for label, value in rows
    )
    return HTMLResponse(
        f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700 '
        f'dark:border-emerald-900/30 dark:bg-emerald-900/10 dark:text-emerald-300 mb-3">{escaped_msg}</div>'
        f'<div class="overflow-hidden rounded-lg border border-slate-200 dark:border-slate-700">'
        f'<table class="min-w-full divide-y divide-slate-200 dark:divide-slate-700">'
        f'<tbody class="divide-y divide-slate-100 bg-white dark:divide-slate-800 dark:bg-slate-900">{row_html}</tbody>'
        f"</table></div>"
    )


@router.post(
    "/olts/{olt_id}/ont-status-by-serial",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_ont_status_by_serial(
    request: Request,
    olt_id: str,
    serial_number: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Lookup an ONT by serial on this OLT and return full OLT-side status."""
    ok, message, status = olt_operations_service.get_ont_status_by_serial(
        db, olt_id, serial_number, request=request
    )
    return _olt_status_by_serial_html(ok=ok, message=message, status=status)


@router.post(
    "/olts/{olt_id}/test-ssh",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_test_ssh_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message, _policy_key = olt_operations_service.test_olt_ssh_connection(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Test SSH Connection",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
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
    ok, message = olt_operations_service.test_olt_snmp_connection(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Test SNMP Connection",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
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
    ok, message, _capabilities = olt_operations_service.test_olt_netconf_connection(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Test NETCONF Connection",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
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

    ok, message, config_xml = olt_operations_service.get_olt_netconf_config(db, olt_id)
    escaped_msg = html_mod.escape(message)
    if not ok:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Get NETCONF Config",
            ok=ok,
            message=message,
        )
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
    "/olts/{olt_id}/ssh-get-config",
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_ssh_get_config(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch OLT running config via SSH/CLI and return as formatted HTML."""
    import html as html_mod

    ok, message, config_text = olt_operations_service.fetch_running_config_ssh_preview(
        db, olt_id, request=request
    )
    escaped_msg = html_mod.escape(message)
    if not ok:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Get SSH Running Config",
            ok=ok,
            message=message,
        )
        return HTMLResponse(
            f'<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            f'dark:border-red-900/30 dark:bg-red-900/10 dark:text-red-300">{escaped_msg}</div>'
        )

    escaped_config = html_mod.escape(config_text)
    return HTMLResponse(
        f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700 '
        f'dark:border-emerald-900/30 dark:bg-emerald-900/10 dark:text-emerald-300 mb-3">'
        f"{escaped_msg}. Retrieved over SSH CLI.</div>"
        f'<pre class="rounded-lg bg-slate-900 p-4 text-xs font-mono text-emerald-400 overflow-x-auto '
        f'max-h-[600px] overflow-y-auto whitespace-pre-wrap">{escaped_config}</pre>'
    )


@router.post(
    "/olts/{olt_id}/sync-onts",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_sync_onts(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message, _stats = olt_snmp_sync_service.sync_onts_from_olt_snmp_tracked(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Sync ONU Telemetry",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
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
    message = quote_plus(
        "Sync ONU telemetry uses POST. Please click Sync ONU Telemetry again."
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status=info&sync_message={message}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/repair-pon-ports",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_repair_pon_ports(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message, _stats = olt_web_topology_service.repair_pon_ports_for_olt_tracked(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Repair PON Ports",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}/repair-pon-ports",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_repair_pon_ports_get_fallback(olt_id: str) -> RedirectResponse:
    message = quote_plus(
        "Repair PON ports uses POST. Please click Repair PON Ports again."
    )
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
    from app.services.network.olt_hardware_discovery import (
        discover_olt_hardware_audited,
    )

    try:
        olt = OLTDevices.get(db, olt_id)
    except HTTPException:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Discover Hardware",
            ok=False,
            message="OLT not found",
        )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?tab=overview&sync_status=error&sync_message={quote_plus('OLT not found')}",
            status_code=303,
        )

    ok, message, _stats = discover_olt_hardware_audited(db, olt, request=request)
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Discover Hardware",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=overview&sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/autofind",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_autofind_scan(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Scan OLT for unregistered ONTs via SSH autofind."""
    ok, message, entries = olt_autofind_service.get_autofind_onts_audited(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Scan Autofind ONTs",
        ok=ok,
        message=message,
    )
    if ok:
        web_network_ont_autofind_service.sync_olt_autofind_entries(
            db,
            olt_id=olt_id,
            entries=entries,
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
        url=f"/admin/network/olts/{olt_id}?tab=provisioning",
        status_code=303,
    )


@router.post(
    "/unconfigured-onts/scan",
    dependencies=[Depends(require_permission("network:write"))],
)
def unconfigured_onts_scan_now() -> RedirectResponse:
    from app.services.queue_adapter import enqueue_task
    from app.tasks.ont_autofind import discover_all_olt_autofind

    enqueue_task(
        discover_all_olt_autofind,
        correlation_id="olt_autofind:all",
        source="admin_network_olts_inventory",
    )
    return RedirectResponse(
        "/admin/network/onts?view=unconfigured&status=success&message="
        + quote_plus("Aggregated OLT autofind scan queued."),
        status_code=303,
    )


@router.get(
    "/unconfigured-onts",
    dependencies=[Depends(require_permission("network:read"))],
)
def unconfigured_onts_list(
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> RedirectResponse:
    target = web_network_ont_autofind_service.build_unconfigured_onts_redirect_url(
        search=search,
        olt_id=olt_id,
        view=view,
        resolution=resolution,
        status=status,
        message=message,
    )
    return RedirectResponse(target, status_code=303)


@router.post(
    "/unconfigured-onts/{candidate_id}/restore",
    dependencies=[Depends(require_permission("network:write"))],
)
def restore_autofind_candidate(
    request: Request, candidate_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message = web_network_ont_autofind_service.restore_candidate_audited(
        db, candidate_id=candidate_id, request=request
    )
    log_network_action_result(
        request=request,
        resource_type="ont_autofind_candidate",
        resource_id=candidate_id,
        action="Restore Autofind Candidate",
        success=ok,
        message=message,
    )
    status = "success" if ok else "error"
    target = web_network_ont_autofind_service.build_unconfigured_onts_feedback_url(
        status=status,
        message=message,
    )
    return RedirectResponse(
        target,
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
    ont_id: str = Form(""),
    return_to: str = Form(""),
    force_reauthorize: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Authorize a discovered ONT on the OLT via SSH."""
    is_htmx = request.headers.get("HX-Request") == "true"

    if not fsp or not serial_number:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Authorize ONT",
            ok=False,
            message="Missing port or serial number",
            metadata={"fsp": fsp, "serial_number": serial_number},
        )
        msg = quote_plus("Missing port or serial number")
        if return_to in (
            "/admin/network/unconfigured-onts",
            "/admin/network/onts",
            "/admin/network/onts?view=unconfigured",
        ):
            target = (
                "/admin/network/onts?view=unconfigured"
                if return_to != "/admin/network/onts"
                else return_to
            )
            separator = "&" if "?" in target else "?"
            return RedirectResponse(
                f"{target}{separator}status=error&message={msg}",
                status_code=303,
            )
        target = f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error&sync_message={msg}"
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers("Missing port or serial number", "error"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    if isinstance(ont_id, str) and ont_id:
        from uuid import UUID

        from app.models.network import OntUnit

        try:
            direct_ont = db.get(OntUnit, UUID(str(ont_id)))
        except ValueError:
            direct_ont = None
        if (
            direct_ont is None
            or str(direct_ont.olt_device_id) != str(olt_id)
            or str(direct_ont.serial_number or "").strip().upper()
            != str(serial_number or "").strip().upper()
        ):
            queue_msg = "ONT authorization scope check failed"
            if is_htmx:
                return Response(
                    status_code=403,
                    headers=_toast_headers(queue_msg, "error"),
                )
            return RedirectResponse(
                f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error"
                f"&sync_message={quote_plus(queue_msg)}",
                status_code=303,
            )

    auth = getattr(getattr(request, "state", None), "auth", None)
    scoped_ont_id = ont_id if isinstance(ont_id, str) else ""
    scope_ok = (
        can_manage_ont_id(auth, db, scoped_ont_id)
        if scoped_ont_id
        else is_internal_operator(auth, db)
    )
    if auth is not None and not scope_ok:
        queue_msg = "ONT authorization scope check failed"
        if is_htmx:
            return Response(
                status_code=403,
                headers=_toast_headers(queue_msg, "error"),
            )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error"
            f"&sync_message={quote_plus(queue_msg)}",
            status_code=303,
        )

    force = str(force_reauthorize or "").lower() in ("true", "1", "on", "yes")
    initiated_by = None
    try:
        initiated_by = actor_label(request)
        # Queue authorization to run in background via Celery
        queue_ok, queue_msg, operation_id = (
            olt_operations_service.queue_authorize_autofind_ont(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
            force_reauthorize=force,
            initiated_by=initiated_by,
            request=request,
        )
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to queue ONT authorization from web route olt_id=%s fsp=%s serial=%s: %s",
            olt_id,
            fsp,
            serial_number,
            exc,
            exc_info=True,
        )
        queue_ok = False
        queue_msg = f"Authorization failed: {exc}"
        operation_id = None
    status = "success" if queue_ok else "error"

    try:
        from app.services.network.olt_web_audit import log_olt_audit_event

        log_olt_audit_event(
            db,
            request=request,
            action="force_authorize_ont" if force else "authorize_ont",
            entity_id=olt_id,
            metadata={
                "result": status,
                "message": queue_msg,
                "fsp": fsp,
                "serial_number": serial_number,
                "force_reauthorize": force,
                "follow_up_operation_id": operation_id,
                "initiated_by": initiated_by,
            },
            status_code=200 if queue_ok else 500,
            is_success=queue_ok,
        )
    except Exception as exc:
        logger.warning(
            "Failed to audit ONT authorization olt_id=%s fsp=%s serial=%s: %s",
            olt_id,
            fsp,
            serial_number,
            exc,
            exc_info=True,
        )
    # For queued operations, send a special event to trigger WebSocket subscription
    if queue_ok and operation_id and is_htmx:
        # Return immediately with operation tracking info
        # The UI will subscribe to WebSocket for real-time updates
        trigger_data = {
            "showToast": {"message": queue_msg, "type": "info"},
            "operationQueued": {
                "operation_id": operation_id,
                "serial_number": serial_number,
                "fsp": fsp,
                "olt_id": olt_id,
            },
        }
        return Response(
            status_code=200,
            headers={
                "HX-Trigger": json.dumps(trigger_data, ensure_ascii=True),
            },
        )

    if return_to in (
        "/admin/network/unconfigured-onts",
        "/admin/network/onts",
        "/admin/network/onts?view=unconfigured",
    ):
        target_base = (
            "/admin/network/onts?view=unconfigured"
            if return_to != "/admin/network/onts"
            else return_to
        )
        separator = "&" if "?" in target_base else "?"
        target = (
            f"{target_base}{separator}status={status}&message={quote_plus(queue_msg)}"
        )
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(queue_msg, status),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    target = (
        f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status={status}"
        f"&sync_message={quote_plus(queue_msg)}"
    )
    if is_htmx:
        return Response(
            status_code=200,
            headers={
                **_toast_headers(queue_msg, status),
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
