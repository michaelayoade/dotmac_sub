"""Admin network ONT web routes."""

import json
import logging
from typing import Any, cast

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import network as network_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services import web_network_service_ports as web_network_service_ports_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import require_permission
from app.services.network import ont_web_forms as ont_web_forms_service
from app.services.network.action_logging import log_network_action_result
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


_form_str = ont_web_forms_service.form_str
_form_uuid_or_none = ont_web_forms_service.form_uuid_or_none
_form_float_or_none = ont_web_forms_service.form_float_or_none
_form_int_or_none = ont_web_forms_service.form_int_or_none
_resolve_splitter_port_id = ont_web_forms_service.resolve_splitter_port_id
_ont_unit_integrity_error_message = (
    ont_web_forms_service.ont_unit_integrity_error_message
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


def _log_ont_action_result(
    *,
    request: Request | None,
    ont_id: str | None,
    action: str,
    ok: bool,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action=action,
        success=ok,
        message=message,
        metadata=metadata,
    )


def _ont_form_dependencies(db: Session, ont: Any | None = None) -> dict:
    """Build all dropdown data needed by the ONT configuration form."""
    return ont_web_forms_service.ont_form_dependencies(db, ont)


@router.get(
    "/onts/{ont_id}/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_detail_preview(
    request: Request,
    ont_id: str,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Temporary preview page for ONT detail layout experiments."""
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
        "configuration",
    }
    active_tab = tab if tab in allowed_tabs else "overview"

    activities = build_audit_activities(db, "ont", str(ont_id))
    try:
        operations = web_network_operations_service.build_operation_history(
            db, "ont", str(ont_id)
        )
    except Exception:
        logger.error(
            "Failed to load operation history for ONT preview %s",
            ont_id,
            exc_info=True,
        )
        operations = []

    context = _base_context(request, db, active_page="onts")
    address_value, contact_value = ont_web_forms_service.split_location_metadata(
        getattr(page_data["ont"], "address_or_comment", None)
    )
    contact_value = str(getattr(page_data["ont"], "contact", None) or contact_value)
    context.update(
        {
            **page_data,
            **_ont_form_dependencies(db, page_data["ont"]),
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
            "preview_mode": True,
            "preview_origin_url": f"/admin/network/onts/{ont_id}",
            "location_address_or_comment": address_value,
            "location_contact": contact_value,
        }
    )
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get(
    "/onts/{ont_id}/location-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_location_details_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve location details modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    return templates.TemplateResponse(
        "admin/network/onts/_location_details_modal.html",
        {
            "request": request,
            **ont_web_forms_service.location_modal_context(db, ont),
        },
    )


@router.post(
    "/onts/{ont_id}/location-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_location_details_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Update ONT location details."""
    result = ont_web_forms_service.update_location_details_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")
    if result.error:
        return templates.TemplateResponse(
            "admin/network/onts/_location_details_modal.html",
            {
                "request": request,
                **ont_web_forms_service.location_modal_context(
                    db,
                    result.ont,
                    error=result.error,
                    form_values=cast(dict[str, Any], result.form_model),
                ),
            },
            status_code=400,
        )

    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/device-info",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_device_info_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve device information modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    return templates.TemplateResponse(
        "admin/network/onts/_device_info_modal.html",
        {
            "request": request,
            **ont_web_forms_service.device_info_modal_context(db, ont),
        },
    )


@router.post(
    "/onts/{ont_id}/device-info",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_device_info_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Update ONT device information."""
    result = ont_web_forms_service.update_device_info_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")
    if result.error:
        return templates.TemplateResponse(
            "admin/network/onts/_device_info_modal.html",
            {
                "request": request,
                **ont_web_forms_service.device_info_modal_context(
                    db,
                    result.ont,
                    error=result.error,
                    form_values=cast(dict[str, Any], result.form_model),
                ),
            },
            status_code=400,
        )

    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/gpon-channel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_gpon_channel_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve GPON channel modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    return templates.TemplateResponse(
        "admin/network/onts/_gpon_channel_modal.html",
        {"request": request, **ont_web_forms_service.gpon_channel_modal_context(ont)},
    )


@router.post(
    "/onts/{ont_id}/gpon-channel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_gpon_channel_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Update ONT GPON channel."""
    result = ont_web_forms_service.update_gpon_channel_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")

    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/wifi-controls",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_wifi_controls(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: WiFi controls with current SSID pre-filled."""
    context = {
        "request": request,
        **ont_web_forms_service.wifi_controls_context(db, ont_id),
    }
    return templates.TemplateResponse("admin/network/onts/_wifi_controls.html", context)


@router.get(
    "/onts/{ont_id}/lan-ports-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_lan_ports_status(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: LAN port status with toggle controls."""
    from app.web.admin.network_onts_actions import _lan_ports_partial_response

    return _lan_ports_partial_response(request, db, ont_id)


@router.get(
    "/onts/{ont_id}/profile-form",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_profile_form(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Profile selection form with available templates."""
    context = {
        "request": request,
        **ont_web_forms_service.profile_form_context(db, ont_id),
    }
    return templates.TemplateResponse("admin/network/onts/_profile_form.html", context)


@router.get(
    "/onts/{ont_id}/firmware-form",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_firmware_form(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Firmware selection form with available images."""
    context = {
        "request": request,
        **ont_web_forms_service.firmware_form_context(db, ont_id),
    }
    return templates.TemplateResponse("admin/network/onts/_firmware_form.html", context)


@router.get(
    "/onts/{ont_id}/provision",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_provision_wizard(
    request: Request,
    ont_id: str,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One-page gated ONT provisioning configuration workflow."""
    context = web_network_onts_service.provision_wizard_context(request, db, ont_id)
    if context.get("error"):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": context["error"]},
            status_code=404,
        )
    if status and message:
        context["provision_feedback"] = {"status": status, "message": message}
    return templates.TemplateResponse("admin/network/onts/provision.html", context)


# -- Service-port management routes --------------------------------------------


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
    resolved_user_vlan, error = web_network_service_ports_service.coerce_user_vlan(
        user_vlan
    )
    if error:
        _log_ont_action_result(
            request=request,
            ont_id=ont_id,
            action="Create Service Port",
            ok=False,
            message=error,
            metadata={"vlan_id": vlan_id, "user_vlan": user_vlan},
        )
        return _service_ports_partial_response(
            request,
            db,
            ont_id,
            toast_message=error,
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
    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Create Service Port",
        ok=ok,
        message=msg,
        metadata={"vlan_id": vlan_id, "gem_index": gem_index},
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
    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Delete Service Port",
        ok=ok,
        message=msg,
        metadata={"service_port_index": index},
    )
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
    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Clone Service Ports",
        ok=ok,
        message=msg,
        metadata={"reference_ont_id": ref_ont_id},
    )
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


# -- Unified Configuration Page Routes -----------------------------------------


@router.get(
    "/onts/{ont_id}/unified-config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_unified_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Unified configuration page with accordion sections."""
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.unified_config_context(db, ont_id))
    return templates.TemplateResponse(
        "admin/network/onts/_unified_config.html", context
    )


@router.get(
    "/onts/{ont_id}/config/service-ports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_service_ports(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Service ports section for unified config."""
    data = web_network_service_ports_service.list_context(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_config_service_ports.html", context
    )


@router.get(
    "/onts/{ont_id}/config/wan",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_wan(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: WAN/PPPoE section for unified config."""
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.wan_config_context(db, ont_id))
    return templates.TemplateResponse("admin/network/onts/_config_wan.html", context)


@router.get(
    "/onts/{ont_id}/config/wifi",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_wifi(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: WiFi section for unified config."""
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.wifi_config_context(db, ont_id))
    return templates.TemplateResponse("admin/network/onts/_config_wifi.html", context)


@router.get(
    "/onts/{ont_id}/config/tr069-profile",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_tr069_profile(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: TR-069 profile binding section for unified config."""
    context = _base_context(request, db, active_page="onts")
    context.update(
        web_network_ont_actions_service.tr069_profile_config_context(db, ont_id)
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_tr069_profile.html", context
    )


@router.get(
    "/onts/{ont_id}/config/lan",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_lan(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: LAN/Ethernet section for unified config."""
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.lan_config_context(db, ont_id))
    return templates.TemplateResponse("admin/network/onts/_config_lan.html", context)


@router.get(
    "/onts/{ont_id}/config/diagnostics",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_config_diagnostics(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Diagnostics section for unified config."""
    context = _base_context(request, db, active_page="onts")
    context.update(
        web_network_ont_actions_service.diagnostics_config_context(db, ont_id)
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_diagnostics.html", context
    )
