"""Admin web routes for ONT actions and runtime tabs."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services.auth_dependencies import require_permission
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


def _sanitize_header_message(message: str) -> str:
    """Sanitize a message for safe inclusion in HTTP headers.

    Removes control characters and non-printable bytes that would cause
    'Invalid HTTP header value' errors from uvicorn/httptools.
    """
    # Remove control characters (0x00-0x1F, 0x7F) and non-ASCII
    return "".join(c for c in message if 0x20 <= ord(c) < 0x7F or c in ("\t",)).strip()


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    safe_message = _sanitize_header_message(message)
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": safe_message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _action_json_response(
    *,
    success: bool,
    message: str,
    action: str,
    waiting: bool = False,
    status_code: int | None = None,
    detail: str | None = None,
) -> JSONResponse:
    """Return a consistent JSON contract for ONT action requests."""
    toast_type = "success" if success else ("info" if waiting else "error")
    phase = "succeeded" if success else ("waiting" if waiting else "failed")
    return JSONResponse(
        {
            "success": success or waiting,
            "message": message,
            "phase": phase,
            "waiting": waiting,
            "operation": {
                "action": action,
                "phase": phase,
                "detail": detail or message,
            },
        },
        status_code=status_code
        if status_code is not None
        else (200 if success else (202 if waiting else 400)),
        headers=_toast_headers(message, toast_type),
    )


def _lan_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    """Return the LAN ports controls partial with current port status."""
    from app.services.network.ont_read import OntReadFacade

    tr069_summary = OntReadFacade.get_tr069_summary(db, ont_id)
    no_tr069 = not tr069_summary.get("available", False)
    ethernet_ports = (
        OntReadFacade.get_ethernet_ports(db, ont_id) if not no_tr069 else []
    )

    context = {
        "request": request,
        "ont_id": ont_id,
        "ethernet_ports": ethernet_ports,
        "no_tr069": no_tr069,
    }
    response = templates.TemplateResponse(
        "admin/network/onts/_lan_ports_controls.html", context
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}},
            ensure_ascii=True,
        )
    return response


@router.post(
    "/onts/{ont_id}/reboot", dependencies=[Depends(require_permission("network:write"))]
)
def ont_reboot(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send reboot command to ONT via GenieACS."""
    result = web_network_ont_actions_service.execute_reboot(db, ont_id, request=request)
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Reboot ONT",
        waiting=result.waiting,
    )


@router.post(
    "/onts/{ont_id}/refresh",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Force status refresh for ONT via GenieACS."""
    result = web_network_ont_actions_service.execute_refresh(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Refresh ONT",
        waiting=result.waiting,
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
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.running_config_context(db, ont_id))
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
    context = {
        "request": request,
        **web_network_ont_actions_service.olt_side_config_context(db, ont_id),
    }
    return templates.TemplateResponse(
        "admin/network/onts/_olt_side_config.html",
        context,
    )


@router.get(
    "/onts/{ont_id}/olt-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_olt_status(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Query the OLT directly for ONT registration state (GPON layer)."""
    context = {
        "request": request,
        **web_network_ont_actions_service.olt_status_context(db, ont_id),
    }
    return templates.TemplateResponse(
        "admin/network/onts/_olt_status.html",
        context,
    )


@router.post(
    "/onts/{ont_id}/return-to-inventory",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_return_to_inventory(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> Response:
    """Deactivate an ONT and reset it to reusable inventory state."""
    result = web_network_ont_actions_service.return_to_inventory_for_web(
        db, ont_id, request=request
    )
    if not result.success and result.message == "ONT not found":
        return JSONResponse(
            {"success": False, "message": "ONT not found"},
            status_code=404,
            headers=_toast_headers("ONT not found", "error"),
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
    result = web_network_ont_actions_service.execute_factory_reset(
        db, ont_id, request=request
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

    result = web_network_ont_actions_service.apply_profile(
        db, ont_id, profile_id, request=request
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

    result = web_network_ont_actions_service.firmware_upgrade(
        db, ont_id, firmware_image_id, request=request
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
    result = web_network_ont_actions_service.set_wifi_ssid(
        db, ont_id, ssid, request=request
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
    result = web_network_ont_actions_service.set_wifi_password(
        db, ont_id, password, request=request
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
) -> Response:
    """Toggle LAN port on ONT via GenieACS TR-069."""
    port_str = request.query_params.get("port", "1")
    enabled_str = request.query_params.get("enabled", "true")
    try:
        port = int(port_str)
    except ValueError:
        port = 1
    enabled = enabled_str.lower() in ("true", "1", "yes")
    result = web_network_ont_actions_service.toggle_lan_port(
        db, ont_id, port, enabled, request=request
    )
    if request.headers.get("HX-Request"):
        return _lan_ports_partial_response(
            request,
            db,
            ont_id,
            toast_message=result.message,
            toast_type="success" if result.success else "error",
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
    form = parse_form_data_sync(request)
    lan_ip = _form_str(form, "lan_ip").strip() or None
    lan_subnet = _form_str(form, "lan_subnet").strip() or None
    result = web_network_ont_actions_service.set_lan_config(
        db,
        ont_id,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        request=request,
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
    password, found = web_network_ont_actions_service.reveal_stored_pppoe_password(
        db, ont_id, request=request
    )
    if not found:
        return JSONResponse({"password": ""}, status_code=404)
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
    result = web_network_ont_actions_service.set_pppoe_credentials(
        db, ont_id, username, password, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Push PPPoE Credentials",
        waiting=getattr(result, "waiting", False),
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
        db, ont_id, host, count, request=request
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
    result = web_network_ont_actions_service.run_traceroute_diagnostic(
        db, ont_id, host, request=request
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
    result = web_network_ont_actions_service.execute_enable_ipv6(
        db, ont_id, request=request
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
    result = web_network_ont_actions_service.execute_connection_request(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Connection Request",
        waiting=result.waiting,
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


@router.get(
    "/onts/{ont_id}/operational-health",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_operational_health(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: operational readiness/actions for the ONT detail page."""
    context: dict[str, object] = {"request": request}
    context.update(web_network_ont_actions_service.operational_health_context(db, ont_id))
    return templates.TemplateResponse(
        "admin/network/onts/_operational_health.html", context
    )


@router.post(
    "/onts/{ont_id}/reconcile",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_reconcile(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Run OLT/ACS reconciliation and return refreshed operational panel."""
    result = web_network_ont_actions_service.reconcile_operational_state(
        db,
        ont_id,
        request=request,
    )
    context: dict[str, object] = {"request": request}
    context.update(
        web_network_ont_actions_service.operational_health_context(
            db,
            ont_id,
            message=result.message,
            message_type="success" if result.success else "error",
        )
    )
    response = templates.TemplateResponse(
        "admin/network/onts/_operational_health.html", context
    )
    response.headers["HX-Trigger"] = json.dumps(
        {
            "showToast": {
                "message": result.message,
                "type": "success" if result.success else "error",
            }
        }
    )
    return response


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
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.iphost_config_context(db, ont_id))
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
    snapshot_context, error_msg = (
        web_network_ont_actions_service.capture_config_snapshot_list_context(
            db,
            ont_id=ont_id,
            label=label,
        )
    )
    context = _base_context(request, db, active_page="onts")
    context.update(snapshot_context)
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
    context = _base_context(request, db, active_page="onts")
    context.update(
        web_network_ont_actions_service.config_snapshot_detail_context(
            db,
            ont_id=ont_id,
            snapshot_id=snapshot_id,
        )
    )
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
    context = _base_context(request, db, active_page="onts")
    context.update(
        web_network_ont_actions_service.delete_config_snapshot_list_context(
            db,
            ont_id=ont_id,
            snapshot_id=snapshot_id,
        )
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html", context
    )
