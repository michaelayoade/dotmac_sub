"""Admin network OLT web routes."""

import logging
from datetime import datetime
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_olt_profiles as web_network_olt_profiles_service
from app.services import web_network_ont_autofind as web_network_ont_autofind_service
from app.services import web_network_onts as web_network_onts_service
from app.services import (
    web_network_pon_interfaces as web_network_pon_interfaces_service,
)
from app.services.auth_dependencies import require_permission
from app.services.ipam_adapter import ipam_adapter
from app.services.network import olt_snmp_sync as olt_snmp_sync_service
from app.services.network import olt_tr069_admin as olt_tr069_admin_service
from app.services.network import olt_web_forms as olt_web_forms_service
from app.services.network import olt_web_topology as olt_web_topology_service
from app.services.network.olt_inventory import get_olt_or_none
from app.services.olt_action_adapter import olt_action_adapter as olt_operations_service
from app.services.olt_detail_adapter import olt_detail_adapter
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


def _olt_config_redirect(olt_id: str, success: bool, message: str) -> RedirectResponse:
    status = "success" if success else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=config&sync_status={status}&sync_message={quote_plus(message)}",
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
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    assert olt is not None  # create_olt_with_audit returns olt when no error
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get(
    "/olts/{olt_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_edit(request: Request, olt_id: str, db: Session = Depends(get_db)):
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
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    assert olt is not None  # update_olt_with_audit returns olt when no error
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
    success, message = ipam_adapter.assign_vlan_to_olt(db, olt_id, vlan_id)
    return _olt_config_redirect(olt_id, success, message)


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
    success, message = ipam_adapter.unassign_vlan_from_olt(db, olt_id, vlan_id)
    return _olt_config_redirect(olt_id, success, message)


@router.post(
    "/olts/{olt_id}/ip-pools/assign",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_assign_ip_pool(
    request: Request,
    olt_id: str,
    pool_id: str = Form(...),
    vlan_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    success, message = ipam_adapter.assign_ip_pool_to_olt(db, olt_id, pool_id, vlan_id)
    return _olt_config_redirect(olt_id, success, message)


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
    success, message = ipam_adapter.unassign_ip_pool_from_olt(db, olt_id, pool_id)
    return _olt_config_redirect(olt_id, success, message)


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
) -> Response:
    from app.services.network.result_adapter import OperationResult

    ok, message, _policy_key = olt_operations_service.test_olt_ssh_connection(
        db, olt_id, request=request
    )
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/test-snmp",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_test_snmp_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> Response:
    from app.services.network.result_adapter import OperationResult

    ok, message = olt_operations_service.test_olt_snmp_connection(
        db, olt_id, request=request
    )
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/test-netconf",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_test_netconf_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> Response:
    from app.services.network.result_adapter import OperationResult

    ok, message, _capabilities = olt_operations_service.test_olt_netconf_connection(
        db, olt_id, request=request
    )
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    return result.to_response(request)


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
    "/olts/{olt_id}/autofind",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_autofind_scan(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Scan OLT for unregistered ONTs via SSH autofind."""
    context = web_network_ont_autofind_service.scan_olt_autofind_results_context(
        db,
        olt_id,
        request=request,
    )
    context["request"] = request
    return templates.TemplateResponse(
        "admin/network/olts/_autofind_results.html",
        context,
    )


@router.get(
    "/unconfigured-onts",
    dependencies=[Depends(require_permission("network:read"))],
)
def unconfigured_onts_list(
    request: Request,
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    params = ["view=unconfigured"]
    if view:
        params.append(f"candidate_view={quote_plus(view)}")
    if resolution:
        params.append(f"resolution={quote_plus(resolution)}")
    if search:
        params.append(f"search={quote_plus(search)}")
    if olt_id:
        params.append(f"olt_id={quote_plus(olt_id)}")
    if status:
        params.append(f"status={quote_plus(status)}")
    if message:
        params.append(f"message={quote_plus(message)}")
    return RedirectResponse(f"/admin/network/onts?{'&'.join(params)}", status_code=303)


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
        source="admin_network_olts",
    )
    return RedirectResponse(
        "/admin/network/onts?view=unconfigured&status=success&message="
        + quote_plus("Aggregated OLT autofind scan queued."),
        status_code=303,
    )


@router.post(
    "/unconfigured-onts/{candidate_id}/restore",
    dependencies=[Depends(require_permission("network:write"))],
)
def restore_autofind_candidate(
    request: Request, candidate_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Restore a disappeared autofind candidate to active state."""
    ok, message = web_network_ont_autofind_service.restore_candidate_audited(
        db, candidate_id=candidate_id, request=request
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/onts?view=unconfigured&status={status}&message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/provision-ont",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_provision_ont(
    request: Request,
    olt_id: str,
    fsp: str = Form(""),
    serial_number: str = Form(""),
    profile_id: str = Form(""),
    force_reauthorize: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Provision an ONT using the coordinated provisioning workflow.

    This uses the ProvisioningCoordinator which handles:
    1. OLT Registration (ont add, service-ports)
    2. Management IP configuration
    3. TR-069 profile binding on OLT
    4. ACS device binding/discovery
    5. Config push via ACS (WiFi, LAN, WAN)

    Tries async (Celery) first, falls back to synchronous execution
    if Celery/Redis is unavailable.

    Args:
        fsp: Frame/Slot/Port location (e.g., "0/1/0")
        serial_number: ONT serial number
        profile_id: Optional provisioning profile ID
        force_reauthorize: If "true" or "1", delete any existing registration
            before provisioning on the specified port.
    """
    from app.services.network.provisioning_coordinator import (
        AsyncProvisioningResult,
        provision_ont_resilient,
    )
    from app.services.network.result_adapter import OperationResult

    if not fsp or not serial_number:
        result = OperationResult.error("Missing port (FSP) or serial number")
        result.redirect_url = f"/admin/network/olts/{olt_id}"
        return result.to_response(request)

    # Parse force_reauthorize checkbox value
    force = str(force_reauthorize or "").lower() in ("true", "1", "on", "yes")
    # Parse profile_id - empty string means no profile
    resolved_profile_id = profile_id.strip() if profile_id else None

    logger.info(
        "provision_ont route: olt_id=%s fsp=%s serial=%s profile_id=%s force=%s",
        olt_id,
        fsp,
        serial_number,
        resolved_profile_id,
        force,
    )

    try:
        prov_result = provision_ont_resilient(
            db,
            olt_id,
            fsp,
            serial_number,
            profile_id=resolved_profile_id,
            force_reauthorize=force,
            request=request,
        )

        # Handle both sync and async results
        if isinstance(prov_result, AsyncProvisioningResult):
            # Async queued result
            if prov_result.queued:
                op_result = OperationResult.queued(
                    prov_result.message,
                    operation_id=prov_result.operation_id,
                    data={"correlation_key": prov_result.correlation_key},
                )
            else:
                op_result = OperationResult.error(prov_result.message)
        else:
            # Sync ProvisioningResult
            op_result = prov_result.to_operation_result()

    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to provision ONT olt_id=%s fsp=%s serial=%s: %s",
            olt_id,
            fsp,
            serial_number,
            exc,
            exc_info=True,
        )
        op_result = OperationResult.error(f"Provisioning failed: {exc}")

    op_result.redirect_url = f"/admin/network/olts/{olt_id}"
    return op_result.to_response(request, default_redirect=f"/admin/network/olts/{olt_id}")


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
    ok, message, profiles, extra = olt_tr069_admin_service.get_tr069_profiles_context(
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
) -> Response:
    """Create a TR-069 server profile on the OLT via SSH."""
    from app.services.network.result_adapter import OperationResult

    ok, message = olt_tr069_admin_service.handle_create_tr069_profile_audited(
        db,
        olt_id,
        profile_name=profile_name.strip(),
        acs_url=acs_url.strip(),
        username=acs_username.strip(),
        password=acs_password.strip(),
        inform_interval=inform_interval,
        request=request,
    )
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    result.redirect_tab = "tr069"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/tr069-profiles/rebind",
    dependencies=[Depends(require_permission("network:write"))],
)
async def olt_tr069_rebind(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Rebind selected ONTs to a TR-069 server profile."""
    from app.services.network.result_adapter import OperationResult

    form = await request.form()
    raw_profile_id = form.get("target_profile_id")
    try:
        target_profile_id = (
            int(raw_profile_id) if isinstance(raw_profile_id, str) else 0
        )
    except (TypeError, ValueError):
        target_profile_id = 0
    ont_ids = [v for v in form.getlist("ont_ids") if isinstance(v, str)]
    if not ont_ids or not target_profile_id:
        result = OperationResult.error("Missing ONT selection or target profile")
        result.redirect_url = f"/admin/network/olts/{olt_id}"
        result.redirect_tab = "tr069"
        return result.to_response(request)

    stats = olt_tr069_admin_service.handle_rebind_tr069_profiles_audited(
        db, olt_id, ont_ids, target_profile_id, request=request
    )
    rebound_val = stats.get("rebound", 0)
    failed_val = stats.get("failed", 0)
    rebound = rebound_val if isinstance(rebound_val, int) else 0
    failed = failed_val if isinstance(failed_val, int) else 0
    errors = stats.get("errors", [])

    message = f"Rebound {rebound} ONT(s) to profile {target_profile_id}"
    if failed:
        message += f", {failed} failed"

    ok = rebound > 0

    result_data = {"rebound": rebound, "failed": failed, "errors": errors}
    result = (
        OperationResult.ok(message, data=result_data)
        if ok
        else OperationResult.error(message, data=result_data)
    )
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    result.redirect_tab = "tr069"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/init-tr069",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_init_tr069(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Create or verify the linked ACS TR-069 profile on the OLT."""
    from app.services.network.result_adapter import OperationResult

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        result = OperationResult.error("OLT not found")
        result.redirect_url = f"/admin/network/olts/{olt_id}"
        return result.to_response(request)

    ok, msg, _profile_id = (
        olt_tr069_admin_service.ensure_tr069_profile_for_linked_acs_audited(
            db, olt, request=request
        )
    )

    result = OperationResult.ok(msg) if ok else OperationResult.error(msg)
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    result.redirect_tab = "tr069"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/firmware-upgrade",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_firmware_upgrade(
    request: Request,
    olt_id: str,
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Trigger firmware upgrade on OLT via SSH."""
    from app.services.network.result_adapter import OperationResult

    if not firmware_image_id:
        result = OperationResult.error("No firmware image selected")
        result.redirect_url = f"/admin/network/olts/{olt_id}"
        return result.to_response(request)

    ok, message = olt_operations_service.trigger_olt_firmware_upgrade(
        db, olt_id, firmware_image_id, request=request
    )
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}"
    return result.to_response(request)


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
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    backups = olt_operations_service.list_olt_backups(
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
    backup = olt_operations_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Backup not found"},
            status_code=404,
        )
    olt = get_olt_or_none(db, str(backup.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    preview = olt_operations_service.read_backup_preview(backup)
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
    backup = olt_operations_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")
    path = olt_operations_service.backup_file_path(backup)
    filename = path.name
    return FileResponse(path=path, filename=filename, media_type="text/plain")


@router.post(
    "/olts/{olt_id}/backups/test-connection",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_test_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> Response:
    from app.services.network.result_adapter import OperationResult

    ok, message = olt_operations_service.test_olt_connection(db, olt_id)
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}/backups"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/backups/test-backup",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_test_backup(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> Response:
    from app.services.network.result_adapter import OperationResult

    backup, message = olt_operations_service.run_test_backup(db, olt_id)
    ok = backup is not None
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}/backups"
    return result.to_response(request)


@router.post(
    "/olts/{olt_id}/backups/ssh-backup",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_backup_ssh(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> Response:
    """Fetch full running config via SSH and save as backup."""
    from app.services.network.result_adapter import OperationResult

    backup, message = olt_operations_service.backup_running_config_ssh(db, olt_id)
    ok = backup is not None
    result = OperationResult.ok(message) if ok else OperationResult.error(message)
    result.redirect_url = f"/admin/network/olts/{olt_id}/backups"
    return result.to_response(request)


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
        backup1, backup2, diff = olt_operations_service.compare_olt_backups(
            db, backup_id_1, backup_id_2
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    olt = get_olt_or_none(db, str(backup1.olt_device_id))
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
    """HTMX partial: ONT physical-link events (PON online/offline/signal)."""
    data = olt_detail_adapter.events_context(db, olt_id=olt_id)
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


# ---------------------------------------------------------------------------
# OLT Lifecycle Management
# ---------------------------------------------------------------------------


@router.get(
    "/olts/{olt_id}/delete-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_delete_preview(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Return HTML partial with deletion impact summary."""
    from app.services.network import olt_lifecycle as olt_lifecycle_service

    impact = olt_lifecycle_service.get_deletion_impact(db, olt_id)
    if not impact:
        return HTMLResponse(
            '<div class="text-rose-600 dark:text-rose-400">OLT not found</div>',
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context["impact"] = impact
    return templates.TemplateResponse(
        "admin/network/olts/_delete_preview.html", context
    )


@router.post(
    "/olts/{olt_id}/set-draining",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_set_draining(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Set OLT to draining status, blocking new ONT authorizations."""
    from app.services.network import olt_lifecycle as olt_lifecycle_service
    from app.web.admin import get_current_user

    user = get_current_user(request)
    actor = getattr(user, "username", "admin") if user else "admin"

    ok, message = olt_lifecycle_service.set_draining(db, olt_id, actor=actor)
    if ok:
        db.commit()
    else:
        db.rollback()

    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/set-active",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_set_active(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Restore OLT to active status, allowing new ONT authorizations."""
    from app.services.network import olt_lifecycle as olt_lifecycle_service
    from app.web.admin import get_current_user

    user = get_current_user(request)
    actor = getattr(user, "username", "admin") if user else "admin"

    ok, message = olt_lifecycle_service.set_active(db, olt_id, actor=actor)
    if ok:
        db.commit()
    else:
        db.rollback()

    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/firmware-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def olt_firmware_upgrade_preview(
    request: Request,
    olt_id: str,
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Dry-run: show current version, target version, compatibility check."""
    import html as html_mod

    from app.services.network import olt_firmware as olt_firmware_service

    if not firmware_image_id:
        return HTMLResponse(
            '<div class="text-rose-600 dark:text-rose-400">No firmware image selected</div>',
            status_code=400,
        )

    result = olt_firmware_service.get_firmware_preview(db, olt_id, firmware_image_id)

    if not result.success:
        escaped_msg = html_mod.escape(result.message)
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 '
            f'dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{escaped_msg}</div>'
        )

    escaped_current = html_mod.escape(result.current_version or "Unknown")
    escaped_target = html_mod.escape(result.target_version or "Unknown")

    return HTMLResponse(
        f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm '
        f'dark:border-emerald-800 dark:bg-emerald-900/20">'
        f'<div class="font-semibold text-emerald-800 dark:text-emerald-300 mb-2">Firmware Upgrade Preview</div>'
        f'<div class="text-emerald-700 dark:text-emerald-400">'
        f'<span class="font-medium">Current:</span> {escaped_current}<br>'
        f'<span class="font-medium">Target:</span> {escaped_target}'
        f"</div>"
        f"</div>"
    )


@router.post(
    "/olts/{olt_id}/firmware-upgrade-verified",
    dependencies=[Depends(require_permission("network:write"))],
)
def olt_firmware_upgrade_verified(
    request: Request,
    olt_id: str,
    firmware_image_id: str = Form(""),
    verify_after: str = Form("true"),
    async_mode: str = Form("true"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Trigger firmware upgrade with verification (optionally async)."""
    if not firmware_image_id:
        msg = quote_plus("No firmware image selected")
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={msg}",
            status_code=303,
        )

    do_verify = verify_after.lower() in ("true", "1", "on", "yes")
    do_async = async_mode.lower() in ("true", "1", "on", "yes")

    if do_async:
        # Queue background task
        from app.services.queue_adapter import enqueue_task
        from app.tasks.olt_firmware import upgrade_firmware_task

        try:
            enqueue_task(
                upgrade_firmware_task,
                args=[olt_id, firmware_image_id],
                kwargs={"verify_after": do_verify},
                correlation_id=f"firmware_upgrade:{olt_id}",
                source="admin_network_olts",
            )
            message = "Firmware upgrade queued. This may take several minutes."
            status = "success"
        except Exception as exc:
            logger.error(
                "Failed to queue firmware upgrade for OLT %s: %s",
                olt_id,
                exc,
                exc_info=True,
            )
            message = f"Failed to queue upgrade: {exc}"
            status = "error"
    else:
        # Run synchronously (may timeout for large upgrades)
        from app.services.network import olt_firmware as olt_firmware_service

        result = olt_firmware_service.upgrade_with_verification_audited(
            db,
            olt_id,
            firmware_image_id,
            dry_run=False,
            verify_after=do_verify,
            request=request,
        )
        message = result.message
        status = "success" if result.success else "error"
        if result.success:
            db.commit()
        else:
            db.rollback()

    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )
