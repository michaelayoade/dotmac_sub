"""Admin web routes for ONT actions and runtime tabs."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_topology as web_network_ont_topology_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services.auth_dependencies import require_permission
from app.services.network.action_logging import log_network_action_result
from app.services.network.ont_scope import can_manage_ont_from_request
from app.services.service_intent_ui_adapter import service_intent_ui_adapter
from app.web.request_parsing import parse_form_data_sync
from app.web.templates import templates

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


def _ensure_ont_write_scope(
    request: Request, db: Session, ont_id: str
) -> JSONResponse | None:
    if can_manage_ont_from_request(request, db, ont_id):
        return None
    return JSONResponse(
        {"success": False, "message": "ONT scope check failed"},
        status_code=403,
        headers=_toast_headers("ONT scope check failed", "error"),
    )


def _action_json_response(
    *,
    success: bool,
    message: str,
    action: str,
    request: Request | None = None,
    ont_id: str | None = None,
    waiting: bool = False,
    status_code: int | None = None,
    detail: str | None = None,
    hx_refresh: bool = False,
) -> JSONResponse:
    """Return a consistent JSON contract for ONT action requests."""
    toast_type = "success" if success else ("info" if waiting else "error")
    phase = "succeeded" if success else ("waiting" if waiting else "failed")
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action=action,
        success=success,
        message=message,
        waiting=waiting,
    )
    headers = _toast_headers(message, toast_type)
    if hx_refresh:
        headers["HX-Refresh"] = "true"
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
        headers=headers,
    )


def _action_result_response(
    *,
    result: object,
    request: Request,
    ont_id: str,
    action: str,
) -> JSONResponse:
    """Return a JSON response for legacy ActionResult handlers."""
    success = bool(getattr(result, "success", False))
    message = str(getattr(result, "message", "Action failed"))
    waiting = bool(getattr(result, "waiting", False))
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action=action,
        success=success,
        message=message,
        waiting=waiting,
    )
    return JSONResponse(
        {"success": success, "message": message},
        status_code=200 if success else 400,
        headers=_toast_headers(message, "success" if success else "error"),
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
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    no_tr069 = not bool(observed_intent.get("available"))
    ethernet_ports = list(observed.get("ethernet_ports", []) or [])

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


def _ethernet_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    """Return the Ethernet ports status partial with current port state."""
    context = _base_context(request, db, active_page="onts")
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    context["ont_id"] = ont_id
    context["ethernet_ports"] = observed.get("ethernet_ports", [])
    response = templates.TemplateResponse(
        "admin/network/onts/_ethernet_ports_partial.html", context
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_reboot(db, ont_id, request=request)
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Reboot ONT",
        request=request,
        ont_id=ont_id,
        waiting=result.waiting,
    )


@router.post(
    "/onts/{ont_id}/reauthorize",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_reauthorize(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Quick re-authorize ONT on OLT (force mode)."""
    from app.models.network import OntUnit
    from app.services.network import olt_operations as olt_operations_service

    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return _action_json_response(
            success=False,
            message="ONT not found",
            action="Re-authorize ONT",
            request=request,
            ont_id=ont_id,
        )

    if not ont.olt_device_id:
        return _action_json_response(
            success=False,
            message="ONT not assigned to an OLT",
            action="Re-authorize ONT",
            request=request,
            ont_id=ont_id,
        )

    # Build FSP from board/port
    fsp = f"{ont.board}/{ont.port}" if ont.board and ont.port else None
    if not fsp:
        return _action_json_response(
            success=False,
            message="ONT missing port assignment (FSP)",
            action="Re-authorize ONT",
            request=request,
            ont_id=ont_id,
        )

    # Call authorize with force=True
    auth_ok, auth_msg, _ = olt_operations_service.authorize_ont(
        db,
        olt_id=str(ont.olt_device_id),
        fsp=fsp,
        serial_number=ont.serial_number or "",
        force_reauthorize=True,
        initiated_by=getattr(getattr(request.state, "user", None), "email", None),
    )

    if auth_ok:
        db.commit()

    return _action_json_response(
        success=auth_ok,
        message=auth_msg,
        action="Re-authorize ONT",
        request=request,
        ont_id=ont_id,
    )


@router.post(
    "/onts/{ont_id}/quick-apply-profile",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_quick_apply_profile(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Apply OLT's default profile to ONT without wizard."""
    from app.models.network import OntUnit

    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return _action_json_response(
            success=False,
            message="ONT not found",
            action="Quick Apply Profile",
            request=request,
            ont_id=ont_id,
        )

    olt = ont.olt_device
    if not olt:
        return _action_json_response(
            success=False,
            message="ONT not assigned to an OLT",
            action="Quick Apply Profile",
            request=request,
            ont_id=ont_id,
        )

    return _action_json_response(
        success=False,
        message="Quick apply profile is obsolete. Edit ONT desired config and run provisioning.",
        action="Quick Apply Profile",
        request=request,
        ont_id=ont_id,
    )


@router.post(
    "/onts/{ont_id}/refresh",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Force status refresh for ONT via GenieACS."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_refresh(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Refresh ONT",
        request=request,
        ont_id=ont_id,
        waiting=result.waiting,
    )


@router.post(
    "/onts/{ont_id}/config/refresh",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_config_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Refresh the stored last-known TR-069 config snapshot."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_config_snapshot_refresh(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Refresh ONT Config Snapshot",
        request=request,
        ont_id=ont_id,
        hx_refresh=result.success,
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
    """Reset an ONT to reusable inventory state."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
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
        # Redirect to the global unconfigured ONT list so the returned device can be re-authorized.
        target = str(
            result.data.get("unconfigured_url")
            if result.data and result.data.get("unconfigured_url")
            else "/admin/network/onts?view=unconfigured"
        )
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_factory_reset(
        db, ont_id, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Factory Reset",
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    if not firmware_image_id:
        return _action_json_response(
            success=False,
            message="No firmware image selected",
            action="Firmware Upgrade",
            request=request,
            ont_id=ont_id,
        )

    result = web_network_ont_actions_service.firmware_upgrade(
        db, ont_id, firmware_image_id, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Firmware Upgrade",
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    # Also accept ssid from query params (used by TR-069 tab Alpine.js modal)
    if not ssid:
        ssid = request.query_params.get("ssid", "")
    result = web_network_ont_actions_service.set_wifi_ssid(
        db, ont_id, ssid, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WiFi SSID",
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.set_wifi_password(
        db, ont_id, password, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WiFi Password",
    )


@router.post(
    "/onts/{ont_id}/wifi-config",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_wifi_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Set WiFi radio, SSID, security, channel, and password via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    enabled_raw = _form_str(form, "enabled").strip().lower()
    enabled = None
    if enabled_raw:
        enabled = enabled_raw in {"true", "1", "yes", "on", "enabled"}
    channel_raw = _form_str(form, "channel").strip()
    channel = int(channel_raw) if channel_raw.isdigit() else None
    result = web_network_ont_actions_service.set_wifi_config(
        db,
        ont_id,
        enabled=enabled,
        ssid=_form_str(form, "ssid").strip() or None,
        password=_form_str(form, "password").strip() or None,
        channel=channel,
        security_mode=_form_str(form, "security_mode").strip() or None,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WiFi Config",
    )


@router.post(
    "/onts/{ont_id}/lan-port",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_toggle_lan_port(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> Response:
    """Toggle LAN port on ONT via GenieACS TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
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
        if not result.success:
            log_network_action_result(
                request=request,
                resource_type="ont",
                resource_id=ont_id,
                action="Toggle LAN Port",
                success=result.success,
                message=result.message,
                waiting=getattr(result, "waiting", False),
            )
        hx_target = request.headers.get("HX-Target", "")
        if hx_target == "ethernet-ports-panel":
            return _ethernet_ports_partial_response(
                request,
                db,
                ont_id,
                toast_message=result.message,
                toast_type="success" if result.success else "error",
            )
        return _lan_ports_partial_response(
            request,
            db,
            ont_id,
            toast_message=result.message,
            toast_type="success" if result.success else "error",
        )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Toggle LAN Port",
    )


@router.post(
    "/onts/{ont_id}/lan-config",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_lan_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set LAN gateway and DHCP server settings on ONT via GenieACS TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    lan_ip = _form_str(form, "lan_ip").strip() or None
    lan_subnet = _form_str(form, "lan_subnet").strip() or None
    dhcp_enabled_raw = _form_str(form, "dhcp_enabled").strip().lower()
    dhcp_enabled = None
    if dhcp_enabled_raw:
        dhcp_enabled = dhcp_enabled_raw in {"true", "1", "yes", "on", "enabled"}
    dhcp_start = _form_str(form, "dhcp_start").strip() or None
    dhcp_end = _form_str(form, "dhcp_end").strip() or None
    result = web_network_ont_actions_service.set_lan_config(
        db,
        ont_id,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        dhcp_enabled=dhcp_enabled,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set LAN Config",
    )


@router.post(
    "/onts/{ont_id}/voip-config",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_voip_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set VoIP enabled status on ONT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    voip_enabled_raw = _form_str(form, "voip_enabled").strip()
    voip_enabled = voip_enabled_raw in {"true", "1", "yes", "on"}

    from app.models.network import OntUnit
    from app.services.adapters import AdapterResult
    from app.services.common import coerce_uuid

    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        result = AdapterResult(ok=False, message="ONT not found")
    else:
        ont.voip_enabled = voip_enabled
        db.flush()
        status = "enabled" if voip_enabled else "disabled"
        result = AdapterResult(ok=True, message=f"VoIP {status} on {ont.serial_number}")

    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set VoIP Config",
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    password, found = web_network_ont_actions_service.reveal_stored_pppoe_password(
        db, ont_id, request=request
    )
    if not found:
        return JSONResponse({"password": ""}, status_code=404)
    return JSONResponse({"password": password})


@router.get(
    "/onts/{ont_id}/running-config",
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_running_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Fetch ONT-specific configuration from the OLT via SSH.

    Returns the service-port and ONT info from the OLT CLI.
    Falls back to cached data if OLT is unreachable.
    """
    from datetime import UTC, datetime

    from app.models.network import OntAssignment, OntUnit
    from app.services.common import coerce_uuid
    from app.services.network.olt_read_cache import olt_cache
    from app.services.network.olt_ssh import run_cli_command
    from app.services.network.serial_utils import parse_ont_id_on_olt

    ont = db.get(OntUnit, coerce_uuid(ont_id))
    if not ont:
        return templates.TemplateResponse(
            "admin/network/onts/_running_config_modal.html",
            {
                "request": request,
                "ont": None,
                "error": "ONT not found",
                "config_text": "",
                "from_cache": False,
                "fetched_at": None,
            },
        )

    active_assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()

    # Try to get the OLT from the ONT or active assignment.
    olt = None
    if ont.olt_device:
        olt = ont.olt_device
    elif active_assignment and active_assignment.pon_port:
        olt = active_assignment.pon_port.olt

    if not olt:
        return templates.TemplateResponse(
            "admin/network/onts/_running_config_modal.html",
            {
                "request": request,
                "ont": ont,
                "error": "No OLT associated with this ONT",
                "config_text": "",
                "from_cache": False,
                "fetched_at": None,
            },
        )

    # Build the ONT-specific command (Huawei style)
    # Get F/S/P from the assignment
    fsp = None
    onu_id = None
    if active_assignment and active_assignment.pon_port:
        pon = active_assignment.pon_port
        fsp = pon.name  # e.g., "0/1/0"
        onu_id = parse_ont_id_on_olt(getattr(ont, "external_id", None))

    # Cache key for this ONT's config
    cache_key = f"ont_config:{ont_id}"
    cached = olt_cache.get(str(olt.id), "cli", cache_key)
    cached_at = None

    config_lines = []
    error_msg = None
    from_cache = False

    # Try to fetch service-port info for this ONT
    if fsp and onu_id:
        # Run display service-port filtered by ONT
        cmd = f"display service-port port {fsp} ont {onu_id}"
        ok, msg, output = run_cli_command(olt, cmd)
        if ok and output.strip():
            config_lines.append(f"# Service Ports ({cmd})")
            config_lines.append(output.strip())
            config_lines.append("")
        elif not ok and cached:
            from_cache = True
            config_lines.append(cached)
            error_msg = "OLT unreachable - showing cached config"
        else:
            error_msg = msg

        # Also try to get ONT info
        cmd2 = f"display ont info {fsp} {onu_id}"
        ok2, msg2, output2 = run_cli_command(olt, cmd2)
        if ok2 and output2.strip():
            config_lines.append(f"# ONT Info ({cmd2})")
            config_lines.append(output2.strip())
    else:
        # Fallback: try by serial number
        cmd = f"display ont info by-sn {ont.serial_number}"
        ok, msg, output = run_cli_command(olt, cmd)
        if ok and output.strip():
            config_lines.append(f"# ONT Info ({cmd})")
            config_lines.append(output.strip())
        elif not ok and cached:
            from_cache = True
            config_lines.append(cached)
            error_msg = "OLT unreachable - showing cached config"
        else:
            error_msg = msg

    config_text = "\n".join(config_lines)

    # Cache successful results
    if config_text and not from_cache and not error_msg:
        olt_cache.set(str(olt.id), "cli", config_text, cache_key)

    return templates.TemplateResponse(
        "admin/network/onts/_running_config_modal.html",
        {
            "request": request,
            "ont": ont,
            "olt": olt,
            "error": error_msg,
            "config_text": config_text,
            "from_cache": from_cache,
            "fetched_at": cached_at if from_cache else datetime.now(UTC),
        },
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.run_ping_diagnostic(
        db, ont_id, host, count, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Run Ping Diagnostic",
        request=request,
        ont_id=ont_id,
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.run_traceroute_diagnostic(
        db, ont_id, host, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Run Traceroute Diagnostic",
        request=request,
        ont_id=ont_id,
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_connection_request(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Connection Request",
        request=request,
        ont_id=ont_id,
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
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    lan_hosts = observed.get("lan_hosts", [])
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
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    ethernet_ports = observed.get("ethernet_ports", [])
    context = _base_context(request, db, active_page="onts")
    context["ont_id"] = ont_id
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
    "/onts/{ont_id}/topology",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_topology(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Fiber path topology for ONT detail page."""
    data = web_network_ont_topology_service.topology_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_topology_partial.html", context
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
    context.update(
        web_network_ont_actions_service.operational_health_context(db, ont_id)
    )
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    ok, msg = web_network_ont_actions_service.execute_omci_reboot(db, ont_id)
    return _action_json_response(
        success=ok,
        message=msg,
        action="OMCI Reboot",
        request=request,
        ont_id=ont_id,
        status_code=200 if ok else 400,
    )


@router.post(
    "/onts/{ont_id}/actions/configure-mgmt-ip",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_configure_mgmt_ip(
    request: Request,
    ont_id: str,
    ip_mode: str = Form(default="dhcp"),
    ip_address: str = Form(default=""),
    subnet: str = Form(default=""),
    gateway: str = Form(default=""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure ONT management IP via OLT IPHOST command."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    ok, msg = web_network_ont_actions_service.configure_management_ip(
        db,
        ont_id,
        ip_mode,
        ip_address=ip_address or None,
        subnet=subnet or None,
        gateway=gateway or None,
    )
    return _action_json_response(
        success=ok,
        message=msg,
        action="Configure Management IP",
        request=request,
        ont_id=ont_id,
        status_code=200 if ok else 400,
    )


@router.post(
    "/onts/{ont_id}/actions/bind-tr069-profile",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_bind_tr069_profile(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bind TR-069 server profile to ONT via OLT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    ok, msg = web_network_ont_actions_service.bind_tr069_profile(db, ont_id)
    return _action_json_response(
        success=ok,
        message=msg,
        action="Bind TR-069 Profile",
        request=request,
        ont_id=ont_id,
        status_code=200 if ok else 400,
    )


# ── Config Snapshots ──────────────────────────────────────────────────────────


@router.get(
    "/onts/{ont_id}/config-snapshots",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_snapshots(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Return the current config snapshot list."""
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "config_snapshots": web_network_ont_actions_service.list_config_snapshots(
                db,
                ont_id,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html",
        context,
    )


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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
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
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
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


# ---------------------------------------------------------------------------
# TR-069 WAN Configuration Routes
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/wan/pppoe-credentials",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_pppoe_credentials(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Push PPPoE credentials to ONT via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    username = _form_str(form, "pppoe_username").strip()
    password = _form_str(form, "pppoe_password").strip()
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()
    ensure_instance = _form_str(form, "ensure_instance").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
        "",
    }

    if not username or not password:
        return _action_json_response(
            success=False,
            message="PPPoE username and password are required",
            action="Set PPPoE Credentials",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None

    result = web_network_ont_actions_service.set_pppoe_credentials(
        db,
        ont_id,
        username=username,
        password=password,
        instance_index=instance_index,
        ensure_instance=ensure_instance,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set PPPoE Credentials",
    )


@router.post(
    "/onts/{ont_id}/wan/dhcp",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_wan_dhcp(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN for DHCP mode via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()
    ensure_instance = _form_str(form, "ensure_instance").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
        "",
    }

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None

    result = web_network_ont_actions_service.set_wan_dhcp(
        db,
        ont_id,
        instance_index=instance_index,
        ensure_instance=ensure_instance,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN DHCP",
    )


@router.post(
    "/onts/{ont_id}/wan/static",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_wan_static(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN for static IP mode via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    ip_address = _form_str(form, "ip_address").strip()
    subnet_mask = _form_str(form, "subnet_mask").strip()
    gateway = _form_str(form, "gateway").strip()
    dns_servers_raw = _form_str(form, "dns_servers").strip()
    instance_index_raw = _form_str(form, "instance_index").strip()

    if not ip_address or not subnet_mask or not gateway:
        return _action_json_response(
            success=False,
            message="IP address, subnet mask, and gateway are required",
            action="Set WAN Static",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    dns_servers = (
        [s.strip() for s in dns_servers_raw.split(",") if s.strip()]
        if dns_servers_raw
        else None
    )

    result = web_network_ont_actions_service.set_wan_static(
        db,
        ont_id,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN Static",
    )


@router.post(
    "/onts/{ont_id}/wan/config",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_wan_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN mode and settings via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    wan_mode = _form_str(form, "wan_mode").strip().lower()
    pppoe_username = _form_str(form, "pppoe_username").strip() or None
    pppoe_password = _form_str(form, "pppoe_password").strip() or None
    ip_address = _form_str(form, "ip_address").strip() or None
    subnet_mask = _form_str(form, "subnet_mask").strip() or None
    gateway = _form_str(form, "gateway").strip() or None
    dns_servers_raw = _form_str(form, "dns_servers").strip()
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()
    ensure_instance = _form_str(form, "ensure_instance").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
        "",
    }

    if wan_mode not in {"pppoe", "dhcp", "static", "bridge"}:
        return _action_json_response(
            success=False,
            message="Invalid WAN mode",
            action="Set WAN Config",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None
    dns_servers = (
        [s.strip() for s in dns_servers_raw.split(",") if s.strip()]
        if dns_servers_raw
        else None
    )

    result = web_network_ont_actions_service.set_wan_config(
        db,
        ont_id,
        wan_mode=wan_mode,
        pppoe_username=pppoe_username,
        pppoe_password=pppoe_password,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
        ensure_instance=ensure_instance,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN Config",
    )


@router.post(
    "/onts/{ont_id}/wan/probe",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_probe_wan_instance(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Probe whether a WAN instance exists on the ONT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_mode = _form_str(form, "wan_mode").strip().lower()

    if wan_mode not in {"pppoe", "dhcp", "static", "bridge"}:
        return _action_json_response(
            success=False,
            message="Select a WAN mode before probing the WAN instance",
            action="Probe WAN Instance",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1

    result = web_network_ont_actions_service.probe_wan_instance(
        db,
        ont_id,
        instance_index=instance_index,
        wan_mode=wan_mode,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Probe WAN Instance",
    )


@router.post(
    "/onts/{ont_id}/wan/ensure-instance",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_ensure_wan_instance(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Ensure a WAN instance exists on the ONT, creating if needed."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_mode = _form_str(form, "wan_mode").strip().lower()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()

    if wan_mode not in {"pppoe", "dhcp", "static", "bridge"}:
        return _action_json_response(
            success=False,
            message="Select a WAN mode before creating the WAN instance",
            action="Ensure WAN Instance",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None

    result = web_network_ont_actions_service.ensure_wan_instance(
        db,
        ont_id,
        instance_index=instance_index,
        wan_mode=wan_mode,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Ensure WAN Instance",
    )


@router.post(
    "/onts/{ont_id}/http-management",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_set_http_management(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Enable or disable HTTP management interface via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    enabled_raw = _form_str(form, "enabled").strip().lower()
    port_raw = _form_str(form, "port").strip()

    enabled = enabled_raw in {"true", "1", "yes", "on", "enabled"}
    port = int(port_raw) if port_raw.isdigit() else 80

    result = web_network_ont_actions_service.set_http_management(
        db,
        ont_id,
        enabled=enabled,
        port=port,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set HTTP Management",
    )


@router.post(
    "/onts/{ont_id}/wan/normalize",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_normalize_wan_structure(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Normalize WAN structure to standard layout via TR-069.

    Deletes non-management WAN instances and establishes consistent WCD layout:
    - WCD1 = Management (TR-069, static IP)
    - WCD2 = Internet (PPPoE/DHCP)

    This ensures TR-069 parameter paths are predictable across all ONTs.
    """
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    form = parse_form_data_sync(request)
    preserve_mgmt_raw = _form_str(form, "preserve_mgmt").strip().lower()
    preserve_mgmt = preserve_mgmt_raw not in {"false", "0", "no", "off"}

    result = web_network_ont_actions_service.normalize_wan_structure(
        db,
        ont_id,
        preserve_mgmt=preserve_mgmt,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Normalize WAN Structure",
    )


# =============================================================================
# ONT Decommission Routes
# =============================================================================


@router.get(
    "/onts/{ont_id}/decommission",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:admin"))],
)
def ont_decommission_preview(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show simplified decommission confirmation modal."""
    from app.services.network.ont_decommission import preview_decommission

    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        context = _base_context(request, db, active_page="onts")
        context["error"] = "ONT scope check failed"
        return templates.TemplateResponse(
            "admin/network/onts/_decommission_modal.html", context
        )

    preview = preview_decommission(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update({
        "preview": preview.to_dict(),
        "ont_id": ont_id,
    })
    return templates.TemplateResponse(
        "admin/network/onts/_decommission_modal.html", context
    )


@router.post(
    "/onts/{ont_id}/decommission",
    dependencies=[Depends(require_permission("network:admin"))],
)
def ont_decommission_execute(
    request: Request,
    ont_id: str,
    reason: str = Form("hardware_fault"),
    remove_from_acs: bool = Form(True),
    deauthorize_on_olt: bool = Form(True),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Execute ONT decommission.

    Simplified flow - single click decommission for NOC efficiency.
    Requires network:admin permission.
    """
    from app.services.network.ont_decommission import decommission_ont_audited

    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    result = decommission_ont_audited(
        db,
        ont_id,
        reason=reason,
        confirm=True,  # Simplified flow - modal click is confirmation
        remove_from_acs=remove_from_acs,
        deauthorize_on_olt=deauthorize_on_olt,
        request=request,
    )

    if result.success:
        db.commit()
        return JSONResponse(
            result.to_dict(),
            status_code=200,
            headers=_toast_headers(result.message, "success"),
        )
    else:
        db.rollback()
        return JSONResponse(
            result.to_dict(),
            status_code=400,
            headers=_toast_headers(result.message, "error"),
        )


# ──────────────────────────────────────────────────────────────────────────────
# ONT Detail Tab Endpoints (for tabbed UI)
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/onts/{ont_id}/hosts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_hosts_tab(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Connected hosts table for the Hosts tab."""
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    lan_hosts = observed.get("lan_hosts", [])

    # Normalize host data for template
    hosts = []
    for host in lan_hosts if isinstance(lan_hosts, list) else []:
        if not isinstance(host, dict):
            continue
        hosts.append({
            "hostname": host.get("host_name") or host.get("HostName") or "-",
            "mac_address": host.get("mac_address") or host.get("MACAddress") or "-",
            "ip_address": host.get("ip_address") or host.get("IPAddress") or "-",
            "interface": host.get("interface_type") or host.get("InterfaceType") or "-",
            "active": str(host.get("active", "")).lower() not in {"false", "0", "no"},
        })

    context = _base_context(request, db, active_page="onts")
    context["hosts"] = hosts
    context["ont_id"] = ont_id
    return templates.TemplateResponse(
        "admin/network/onts/_hosts_table.html", context
    )


@router.get(
    "/onts/{ont_id}/tr069-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_tr069_status_modal(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069/ACS status modal content."""
    data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_tr069_status_modal.html", context
    )


@router.get(
    "/onts/{ont_id}/logs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_logs_tab(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Device logs for the Logs tab."""
    # For now, return a placeholder - actual log fetching would require
    # TR-069 DeviceLog parameter queries
    context = _base_context(request, db, active_page="onts")
    context["ont_id"] = ont_id
    context["logs"] = []  # Placeholder - would be populated from TR-069
    return templates.TemplateResponse(
        "admin/network/onts/_logs_table.html", context
    )


@router.get(
    "/onts/{ont_id}/refresh-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_refresh_status_get(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Refresh ONT status from OLT and TR-069 (GET for HTMX compatibility)."""
    from app.services.network.ont_actions import OntActions

    result = OntActions.refresh_status(db, ont_id)
    if result.success:
        db.commit()
    return JSONResponse(
        {"success": result.success, "message": result.message},
        headers=_toast_headers(
            result.message, "success" if result.success else "error"
        ),
    )
