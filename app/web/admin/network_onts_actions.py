"""Admin web routes for ONT actions and runtime tabs."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.services import network as network_service
from app.services import web_admin as web_admin_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services import web_network_onts as web_network_onts_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission

_logger = logging.getLogger(__name__)

try:
    from app.services.network.ont_config_snapshots import ont_config_snapshots
except ImportError:
    _logger.error(
        "Failed to import ont_config_snapshots — config snapshot routes will be unavailable",
        exc_info=True,
    )
    ont_config_snapshots = None  # type: ignore[assignment]
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-ont-actions"])


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _action_json_response(
    *,
    success: bool,
    message: str,
    action: str,
    status_code: int | None = None,
    detail: str | None = None,
) -> JSONResponse:
    """Return a consistent JSON contract for ONT action requests."""
    toast_type = "success" if success else "error"
    phase = "succeeded" if success else "failed"
    return JSONResponse(
        {
            "success": success,
            "message": message,
            "phase": phase,
            "operation": {
                "action": action,
                "phase": phase,
                "detail": detail or message,
            },
        },
        status_code=status_code if status_code is not None else (200 if success else 400),
        headers=_toast_headers(message, toast_type),
    )


def _actor_context(request: Request) -> tuple[dict | None, str | None, str]:
    current_user = web_admin_service.get_current_user(request)
    actor_id = (
        str(current_user.get("actor_id") or current_user.get("id"))
        if current_user
        else None
    )
    actor_name = current_user.get("name", "unknown") if current_user else "system"
    return current_user, actor_id, actor_name


@router.post(
    "/onts/{ont_id}/reboot", dependencies=[Depends(require_permission("network:write"))]
)
def ont_reboot(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send reboot command to ONT via GenieACS."""
    current_user, actor_id, actor_name = _actor_context(request)
    result = web_network_ont_actions_service.execute_reboot(
        db, ont_id, initiated_by=actor_name
    )
    log_audit_event(
        db=db,
        request=request,
        action="reboot",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={"success": result.success, "message": result.message},
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Reboot ONT",
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
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="refresh",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={"success": result.success},
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Refresh ONT",
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


@router.get(
    "/onts/{ont_id}/olt-config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_olt_side_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch ONT config from OLT side via SSH (works without GenieACS)."""
    import html as html_mod

    result = web_network_ont_actions_service.fetch_olt_side_config(db, ont_id)
    if not result.success:
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 p-4 '
            f'dark:border-rose-800/50 dark:bg-rose-900/20">'
            f'<p class="text-sm text-rose-700 dark:text-rose-300">'
            f"{html_mod.escape(result.message)}</p></div>"
        )

    sections = result.data or {}
    parts = [
        '<div class="space-y-4">',
        f'<p class="text-xs text-slate-500 dark:text-slate-400">{html_mod.escape(result.message)}</p>',
    ]
    section_labels = {
        "ont_info": "ONT Info",
        "ont_wan": "WAN Info",
        "service_ports": "Service Ports",
    }
    for key, label in section_labels.items():
        content = sections.get(key)
        if not content:
            continue
        parts.append(
            f'<div>'
            f'<h4 class="text-xs font-semibold uppercase tracking-wide text-slate-500 '
            f'dark:text-slate-400 mb-2">{label}</h4>'
            f'<pre class="whitespace-pre-wrap break-words text-xs font-mono '
            f'text-emerald-800 dark:text-emerald-300 bg-slate-900 dark:bg-slate-950 '
            f'rounded-lg p-3 overflow-x-auto">{html_mod.escape(content)}</pre>'
            f"</div>"
        )
    parts.append("</div>")
    return HTMLResponse("\n".join(parts))


@router.get(
    "/onts/{ont_id}/olt-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_olt_status(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Query the OLT directly for ONT registration state (GPON layer)."""
    import html as html_mod

    result = web_network_ont_actions_service.fetch_olt_status(db, ont_id)
    if not result["success"]:
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 p-4 '
            f'dark:border-rose-800/50 dark:bg-rose-900/20">'
            f'<p class="text-sm text-rose-700 dark:text-rose-300">'
            f"{html_mod.escape(result['message'])}</p></div>"
        )

    entry = result.get("entry")
    if not entry:
        return HTMLResponse(
            '<div class="rounded-lg border border-slate-200 bg-slate-50 p-4 '
            'dark:border-slate-700 dark:bg-slate-800">'
            '<p class="text-sm text-slate-500 dark:text-slate-400">'
            "No status data returned.</p></div>"
        )

    run_state = entry["run_state"]
    state_color = (
        "emerald" if run_state == "online"
        else "rose" if run_state == "offline"
        else "slate"
    )

    rows = [
        ("Run State", entry["run_state"]),
        ("Config State", entry["config_state"]),
        ("Match State", entry["match_state"]),
        ("Serial", entry["serial_number"]),
        ("F/S/P", entry["fsp"]),
        ("ONT-ID", str(entry["ont_id"])),
        ("Last Down Cause", entry["last_down_cause"] or "—"),
        ("Last Down Time", entry["last_down_time"] or "—"),
        ("Last Up Time", entry["last_up_time"] or "—"),
        ("Description", entry["description"] or "—"),
    ]

    html_rows = []
    for label, value in rows:
        escaped_val = html_mod.escape(str(value))
        if label == "Run State":
            escaped_val = (
                f'<span class="inline-flex items-center rounded-full px-2 py-0.5 '
                f"text-xs font-medium bg-{state_color}-100 text-{state_color}-800 "
                f'dark:bg-{state_color}-900/40 dark:text-{state_color}-300">'
                f"{escaped_val}</span>"
            )
        html_rows.append(
            f"<tr>"
            f'<td class="py-1.5 pr-4 text-xs font-medium text-slate-500 '
            f'dark:text-slate-400 whitespace-nowrap">{html_mod.escape(label)}</td>'
            f'<td class="py-1.5 text-sm text-slate-900 dark:text-white font-mono">'
            f"{escaped_val}</td>"
            f"</tr>"
        )

    return HTMLResponse(
        f'<div class="space-y-3">'
        f'<p class="text-xs text-slate-500 dark:text-slate-400">'
        f"{html_mod.escape(result['message'])}</p>"
        f'<table class="w-full">'
        f'<tbody class="divide-y divide-slate-100 dark:divide-slate-700/50">'
        f"{''.join(html_rows)}"
        f"</tbody></table></div>"
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
        _current_user, actor_id, _actor_name = _actor_context(request)
        log_audit_event(
            db=db,
            request=request,
            action="return_to_inventory",
            entity_type="ont",
            entity_id=str(ont.id),
            actor_id=actor_id,
            metadata={"serial_number": ont.serial_number},
        )

    if result.success:
        target = "/admin/network/onts"
        if request.headers.get("hx-request") == "true":
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(result.message, "success"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    return Response(
        status_code=400,
        headers=_toast_headers(result.message, "error"),
    )


@router.post(
    "/onts/{ont_id}/factory-reset",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_factory_reset(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send factory reset command to ONT via GenieACS."""
    _current_user, actor_id, actor_name = _actor_context(request)
    result = web_network_ont_actions_service.execute_factory_reset(
        db, ont_id, initiated_by=actor_name
    )
    log_audit_event(
        db=db,
        request=request,
        action="factory_reset",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={"success": result.success, "message": result.message},
    )
    status_code = 200 if result.success else 400
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
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="apply_profile",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
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
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="firmware_upgrade",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
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
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
    ssid: str = Form(""),
) -> JSONResponse:
    """Set WiFi SSID on ONT via GenieACS TR-069."""
    # Also accept ssid from query params (used by TR-069 tab Alpine.js modal)
    if not ssid:
        ssid = request.query_params.get("ssid", "")
    result = web_network_ont_actions_service.set_wifi_ssid(db, ont_id, ssid)
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="set_wifi_ssid",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={"success": result.success, "ssid": ssid},
    )
    status_code = 200 if result.success else 400
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
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="set_wifi_password",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={"success": result.success},
    )
    status_code = 200 if result.success else 400
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
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="toggle_lan_port",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={
            "success": result.success,
            "port": port,
            "enabled": enabled,
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
    "/onts/{ont_id}/lan-config",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_lan_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set LAN IP/subnet on ONT via GenieACS TR-069."""
    from app.services.network.ont_actions import OntActions

    form = parse_form_data_sync(request)
    lan_ip = _form_str(form, "lan_ip").strip() or None
    lan_subnet = _form_str(form, "lan_subnet").strip() or None
    result = OntActions.set_lan_config(db, ont_id, lan_ip=lan_ip, lan_subnet=lan_subnet)
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="set_lan_config",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={
            "success": result.success,
            "lan_ip": lan_ip,
            "lan_subnet": lan_subnet,
        },
    )
    status_code = 200 if result.success else 400
    headers = _toast_headers(result.message, "success" if result.success else "error")
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.get(
    "/onts/{ont_id}/pppoe-password",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_reveal_pppoe_password(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Decrypt and return the stored PPPoE password for verification."""
    from app.models.network import OntUnit

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return JSONResponse({"password": ""}, status_code=404)

    password = web_network_ont_actions_service.resolve_stored_pppoe_password(db, ont_id)

    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="reveal_pppoe_password",
        entity_type="ont",
        entity_id=ont_id,
        actor_id=actor_id,
        metadata={"username": ont.pppoe_username or ""},
    )
    return JSONResponse({"password": password})


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
    _current_user, actor_id, actor_name = _actor_context(request)
    result = web_network_ont_actions_service.set_pppoe_credentials(
        db, ont_id, username, password, initiated_by=actor_name
    )
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
        status_code=200 if result.success else 400,
        is_success=result.success,
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Push PPPoE Credentials",
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
    result = web_network_ont_actions_service.run_ping_diagnostic(
        db, ont_id, host, count
    )
    _current_user, actor_id, _actor_name = _actor_context(request)
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
        status_code=200 if result.success else 400,
        is_success=result.success,
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Run Ping Diagnostic",
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
    result = web_network_ont_actions_service.run_traceroute_diagnostic(db, ont_id, host)
    _current_user, actor_id, _actor_name = _actor_context(request)
    log_audit_event(
        db=db,
        request=request,
        action="traceroute_diagnostic",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=actor_id,
        metadata={"result": "success" if result.success else "error", "host": host},
        status_code=200 if result.success else 400,
        is_success=result.success,
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Run Traceroute Diagnostic",
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
    _current_user, _actor_id, actor_name = _actor_context(request)
    result = web_network_ont_actions_service.execute_enable_ipv6(
        db, ont_id, initiated_by=actor_name
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Enable IPv6",
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

    _current_user, _actor_id, actor_name = _actor_context(request)
    result = send_connection_request_tracked(db, ont_id, initiated_by=actor_name)
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Connection Request",
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
    return _action_json_response(
        success=ok,
        message=msg,
        action="OMCI Reboot",
        status_code=200 if ok else 400,
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
    return _action_json_response(
        success=ok,
        message=msg,
        action="Configure Management IP",
        status_code=200 if ok else 400,
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
    return _action_json_response(
        success=ok,
        message=msg,
        action="Bind TR-069 Profile",
        status_code=200 if ok else 400,
    )


@router.get(
    "/onts/{ont_id}/iphost-config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
@router.get(
    "/onts/{ont_id}/provisioning-support",
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


# ── Config Snapshots ──────────────────────────────────────────────────────────


@router.post(
    "/onts/{ont_id}/config-snapshot",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_capture_config_snapshot(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Capture a new config snapshot from TR-069 and return updated list."""
    form = parse_form_data_sync(request)
    label = _form_str(form, "label").strip() or None
    if ont_config_snapshots is None:
        raise HTTPException(status_code=501, detail="Config snapshots not available")
    error_msg = None
    try:
        ont_config_snapshots.capture(db, ont_id, label=label)
    except HTTPException as exc:
        error_msg = exc.detail
    snapshots = ont_config_snapshots.list_for_ont(db, ont_id, limit=5)
    context = _base_context(request, db, active_page="onts")
    context["ont_id"] = ont_id
    context["config_snapshots"] = snapshots
    response = templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html", context
    )
    if error_msg:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": error_msg, "type": "error"}}
        )
    else:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": "Config snapshot saved.", "type": "success"}}
        )
    return response


@router.get(
    "/onts/{ont_id}/config-snapshots/{snapshot_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_view_config_snapshot(
    request: Request,
    ont_id: str,
    snapshot_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """View a single config snapshot detail in a slide-over."""
    if ont_config_snapshots is None:
        raise HTTPException(status_code=501, detail="Config snapshots not available")
    snapshot = ont_config_snapshots.get(db, snapshot_id, ont_id=ont_id)
    context = _base_context(request, db, active_page="onts")
    context["snapshot"] = snapshot
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_detail.html", context
    )


@router.delete(
    "/onts/{ont_id}/config-snapshots/{snapshot_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_delete_config_snapshot(
    request: Request,
    ont_id: str,
    snapshot_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Delete a config snapshot and return updated list."""
    if ont_config_snapshots is None:
        raise HTTPException(status_code=501, detail="Config snapshots not available")
    ont_config_snapshots.delete(db, snapshot_id, ont_id=ont_id)
    snapshots = ont_config_snapshots.list_for_ont(db, ont_id, limit=5)
    context = _base_context(request, db, active_page="onts")
    context["ont_id"] = ont_id
    context["config_snapshots"] = snapshots
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html", context
    )
