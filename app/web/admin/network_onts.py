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
from app.models.network import (
    GponChannel,
)
from app.services import network as network_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services import web_network_service_ports as web_network_service_ports_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import require_permission
from app.services.network import ont_web_forms as ont_web_forms_service
from app.services.network.ont_tr069 import TR069Summary
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])
_LOCATION_CONTACT_MARKER = "\n---\nLocation Contact: "


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


def _ont_form_dependencies(db: Session, ont: Any | None = None) -> dict:
    """Build all dropdown data needed by the ONT configuration form."""
    return ont_web_forms_service.ont_form_dependencies(db, ont)


def _split_location_metadata(value: str | None) -> tuple[str, str]:
    """Split address/comment text from embedded location contact metadata."""
    raw = (value or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("---\nLocation Contact: "):
        return "", raw.removeprefix("---\nLocation Contact: ").strip()
    if _LOCATION_CONTACT_MARKER not in raw:
        return raw, ""
    address_part, contact_part = raw.split(_LOCATION_CONTACT_MARKER, 1)
    return address_part.strip(), contact_part.strip()


def _build_location_address_or_comment(address: str, contact: str) -> str | None:
    """Persist only the address/comment now that contact has its own column."""
    return ont_web_forms_service.build_location_address_or_comment(address, contact)


def _location_modal_context(
    request: Request,
    db: Session,
    ont: Any,
    *,
    error: str | None = None,
    form_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build modal context for ONT location details editing."""
    address_value, contact_value = _split_location_metadata(
        getattr(ont, "address_or_comment", None)
    )
    contact_value = str(getattr(ont, "contact", None) or contact_value)
    initial_port_number = (
        ont.splitter_port_rel.port_number
        if getattr(ont, "splitter_port_rel", None) is not None
        else None
    )
    form = {
        "zone_id": str(form_values["zone_id"])
        if form_values and form_values.get("zone_id")
        else str(ont.zone_id)
        if getattr(ont, "zone_id", None)
        else "",
        "splitter_id": str(form_values["splitter_id"])
        if form_values and form_values.get("splitter_id")
        else str(ont.splitter_id)
        if getattr(ont, "splitter_id", None)
        else "",
        "splitter_port_number": str(form_values["splitter_port_number"])
        if form_values and form_values.get("splitter_port_number") is not None
        else str(initial_port_number)
        if initial_port_number is not None
        else "",
        "name": str(form_values["name"])
        if form_values and form_values.get("name") is not None
        else str(getattr(ont, "name", "") or ""),
        "address_or_comment": str(form_values["address_or_comment"])
        if form_values and form_values.get("address_or_comment") is not None
        else address_value,
        "contact": str(form_values["contact"])
        if form_values and form_values.get("contact") is not None
        else contact_value,
        "gps_latitude": str(form_values["gps_latitude"])
        if form_values and form_values.get("gps_latitude") is not None
        else str(getattr(ont, "gps_latitude", "") or ""),
        "gps_longitude": str(form_values["gps_longitude"])
        if form_values and form_values.get("gps_longitude") is not None
        else str(getattr(ont, "gps_longitude", "") or ""),
    }
    return {
        "request": request,
        "ont": ont,
        "zones": web_network_onts_service.get_zones(db),
        "splitters": web_network_onts_service.get_splitters(db),
        "form": form,
        "error": error,
    }


def _device_info_modal_context(
    request: Request,
    db: Session,
    ont: Any,
    *,
    error: str | None = None,
    form_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build modal context for ONT device information editing."""
    form = {
        "vendor": str(form_values["vendor"])
        if form_values and form_values.get("vendor") is not None
        else str(getattr(ont, "vendor", "") or ""),
        "model": str(form_values["model"])
        if form_values and form_values.get("model") is not None
        else str(getattr(ont, "model", "") or ""),
        "firmware_version": str(form_values["firmware_version"])
        if form_values and form_values.get("firmware_version") is not None
        else str(getattr(ont, "firmware_version", "") or ""),
        "onu_type_id": str(form_values["onu_type_id"])
        if form_values and form_values.get("onu_type_id")
        else str(ont.onu_type_id)
        if getattr(ont, "onu_type_id", None)
        else "",
    }
    return {
        "request": request,
        "ont": ont,
        "onu_types": web_network_onts_service.get_onu_types(db),
        "form": form,
        "error": error,
    }


def _gpon_channel_modal_context(
    request: Request,
    ont: Any,
    *,
    error: str | None = None,
    form_value: str | None = None,
) -> dict[str, Any]:
    """Build modal context for GPON channel editing."""
    current_channel = (
        ont.gpon_channel.value
        if getattr(ont, "gpon_channel", None) is not None
        and getattr(ont.gpon_channel, "value", None) is not None
        else getattr(ont, "gpon_channel", None)
    )
    return {
        "request": request,
        "ont": ont,
        "gpon_channels": [e.value for e in GponChannel],
        "form": {"gpon_channel": form_value if form_value is not None else (current_channel or "gpon")},
        "error": error,
    }


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
    address_value, contact_value = _split_location_metadata(
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
        _location_modal_context(request, db, ont),
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
            _location_modal_context(
                request,
                db,
                result.ont,
                error=result.error,
                form_values=cast(dict[str, Any], result.form_model),
            ),
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
        _device_info_modal_context(request, db, ont),
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
            _device_info_modal_context(
                request,
                db,
                result.ont,
                error=result.error,
                form_values=cast(dict[str, Any], result.form_model),
            ),
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
        _gpon_channel_modal_context(request, ont),
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
    from app.services.network.ont_read import OntReadFacade

    tr069_summary = OntReadFacade.get_tr069_summary(db, ont_id)
    current_ssid = None
    no_tr069 = True
    if tr069_summary.get("available"):
        no_tr069 = False
        wireless = tr069_summary.get("wireless") or {}
        current_ssid = wireless.get("SSID") or wireless.get("ssid")

    context = {
        "request": request,
        "ont_id": ont_id,
        "current_ssid": current_ssid,
        "no_tr069": no_tr069,
    }
    return templates.TemplateResponse(
        "admin/network/onts/_wifi_controls.html", context
    )

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
    context = {"request": request, **ont_web_forms_service.profile_form_context(db, ont_id)}
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    ont_vendor = str(getattr(ont, "vendor", "") or "").strip() if ont else ""
    available_firmware = web_network_onts_service.get_active_firmware_images(
        db,
        vendor_contains=ont_vendor or None,
        limit=20,
    )

    context = {
        "request": request,
        "ont_id": ont_id,
        "available_firmware": available_firmware,
        "ont_vendor": ont_vendor,
    }
    return templates.TemplateResponse("admin/network/onts/_firmware_form.html", context)


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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        raise HTTPException(status_code=404, detail="ONT not found")

    # Get basic info for summary badges (without full data load)
    ok, msg, iphost_config = web_network_ont_actions_service.fetch_iphost_config(
        db, ont_id
    )
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )

    # Build summary info for accordion headers
    initial_form = ont_web_forms_service.initial_iphost_form(ont, iphost_config)
    mgmt_ip_summary = {
        "mode": initial_form.get("ip_mode"),
        "vlan": initial_form.get("vlan_id"),
        "ip": initial_form.get("ip_address") if initial_form.get("ip_mode") == "static" else None,
    }

    # Get service ports count (lightweight check)
    try:
        sp_data = web_network_service_ports_service.list_context(db, ont_id)
        service_ports_count = len(sp_data.get("service_ports", []))
    except Exception:
        service_ports_count = 0

    # Get TR-069 summary if available
    wan_summary = {
        "pppoe_user": getattr(ont, "pppoe_username", None),
        "wan_ip": getattr(ont, "observed_wan_ip", None),
        "status": getattr(ont, "observed_pppoe_status", None),
    }

    # WiFi summary from observed data
    wifi_summary = {
        "ssid": None,  # Would need TR-069 call to get current SSID
    }

    # Check if TR-069 is available
    has_tr069 = bool(getattr(ont, "mac_address", None))

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": ont,
            "iphost_config": iphost_config,
            "iphost_ok": ok,
            "iphost_msg": msg,
            "initial_iphost_form": initial_form,
            "vlans": vlans,
            "tr069_profiles": tr069_profiles,
            "tr069_profiles_error": tr069_profiles_error,
            "mgmt_ip_summary": mgmt_ip_summary,
            "service_ports_count": service_ports_count,
            "wan_summary": wan_summary,
            "wifi_summary": wifi_summary,
            "has_tr069": has_tr069,
        }
    )
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return templates.TemplateResponse(
            "admin/network/onts/_config_wan.html",
            {"request": request, "error": "ONT not found"},
        )

    # Try to get TR-069 data
    tr069_data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = cast(TR069Summary | None, tr069_data.get("tr069"))

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.available,
            "wan_info": tr069.wan if tr069 else None,
            "current_pppoe_user": (tr069.wan or {}).get("Username") if tr069 else None,
        }
    )
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return templates.TemplateResponse(
            "admin/network/onts/_config_wifi.html",
            {"request": request, "error": "ONT not found"},
        )

    # Try to get TR-069 data
    tr069_data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = cast(TR069Summary | None, tr069_data.get("tr069"))

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.available,
            "wireless_info": tr069.wireless if tr069 else None,
            "current_ssid": (tr069.wireless or {}).get("SSID") if tr069 else None,
        }
    )
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return templates.TemplateResponse(
            "admin/network/onts/_config_tr069_profile.html",
            {"request": request, "error": "ONT not found"},
        )

    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_profiles": tr069_profiles,
            "tr069_profiles_error": tr069_profiles_error,
            "current_profile": None,  # Would need to fetch from OLT
            "current_profile_id": None,
        }
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return templates.TemplateResponse(
            "admin/network/onts/_config_lan.html",
            {"request": request, "error": "ONT not found"},
        )

    # Try to get TR-069 data
    tr069_data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = cast(TR069Summary | None, tr069_data.get("tr069"))

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.available,
            "lan_info": tr069.lan if tr069 else None,
            "ethernet_ports": tr069.ethernet_ports if tr069 else None,
            "lan_hosts": tr069.lan_hosts if tr069 else None,
        }
    )
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    if not ont:
        return templates.TemplateResponse(
            "admin/network/onts/_config_diagnostics.html",
            {"request": request, "error": "ONT not found"},
        )

    # Check TR-069 availability
    tr069_data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = cast(TR069Summary | None, tr069_data.get("tr069"))

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.available,
        }
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_diagnostics.html", context
    )
