"""Admin network ONT web routes."""

import json
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
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
    OnuMode,
    WanMode,
)
from app.services import network as network_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
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
    """Build all dropdown data needed by the ONT configuration form."""
    deps = web_network_onts_service.ont_form_dependencies(db, ont)
    deps["gpon_channels"] = [e.value for e in GponChannel]
    deps["onu_modes"] = [e.value for e in OnuMode]
    return deps


def _ont_has_active_assignment(db: Session, ont_id: str) -> bool:
    """Return True when the ONT currently has an active assignment."""
    return web_network_ont_assignments_service.has_active_assignment(db, ont_id)


def _form_uuid_or_none(form: FormData, key: str) -> uuid.UUID | None:
    """Extract a UUID from form data, returning None if empty."""
    from uuid import UUID

    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


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


def _normalize_iphost_key(value: str) -> str:
    """Normalize OLT IPHOST labels for loose matching."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _iphost_value(
    config: dict[str, str],
    *patterns: str,
) -> str | None:
    """Return the first IPHOST value whose normalized key contains a pattern."""
    if not config:
        return None
    normalized = {
        _normalize_iphost_key(key): str(value).strip()
        for key, value in config.items()
        if value is not None
    }
    for pattern in patterns:
        needle = _normalize_iphost_key(pattern)
        for key, value in normalized.items():
            if needle in key:
                return value
    return None


def _initial_iphost_form(ont: Any, config: dict[str, str]) -> dict[str, str]:
    """Build Management IP form defaults from live OLT config first, DB fallback second."""
    live_mode = (_iphost_value(config, "ip mode", "address mode", "mode") or "").lower()
    if "static" in live_mode:
        ip_mode = "static"
    elif "dhcp" in live_mode:
        ip_mode = "dhcp"
    elif getattr(ont, "mgmt_ip_mode", None) and getattr(ont.mgmt_ip_mode, "value", None) == "static_ip":
        ip_mode = "static"
    else:
        ip_mode = "dhcp"

    live_vlan = _iphost_value(config, "vlan", "vlan id") or ""
    vlan_digits = re.search(r"\d+", live_vlan)
    live_ip = _iphost_value(config, "ip address", "ip") or ""
    subnet = _iphost_value(config, "subnet mask", "mask", "subnet") or ""
    gateway = _iphost_value(config, "gateway") or ""

    fallback_vlan = ""
    if getattr(ont, "mgmt_vlan", None) and getattr(ont.mgmt_vlan, "tag", None) is not None:
        fallback_vlan = str(ont.mgmt_vlan.tag)

    return {
        "ip_mode": ip_mode,
        "vlan_id": vlan_digits.group(0) if vlan_digits else fallback_vlan,
        "ip_address": live_ip or str(getattr(ont, "mgmt_ip_address", "") or ""),
        "subnet": subnet,
        "gateway": gateway,
    }


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
        summary = (
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 '
            f'dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{error}</div>'
        )
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
        # Imported / external network inventory fields
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
            "Failed to load operation history for ONT %s", ont_id, exc_info=True
        )
        operations = []
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
        }
    )
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


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
    context.update(
        {
            **page_data,
            **_ont_form_dependencies(db, page_data["ont"]),
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
            "preview_mode": True,
            "preview_origin_url": f"/admin/network/onts/{ont_id}",
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
        # Imported / external network inventory fields
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


# -- ONU Mode Modal -----------------------------------------------------------


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


# -- ONT Remote Actions -------------------------------------------------------


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
    """Release an ONT from the OLT and reset it to reusable inventory state."""
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
    """Apply a profile template to an ONT as an explicit manual action."""
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


# -- ONT management IP / OMCI / TR-069 routes ---------------------------------


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
            "initial_iphost_form": _initial_iphost_form(ont, config),
            "vlans": vlans,
            "tr069_profiles": tr069_profiles,
            "tr069_profiles_error": tr069_profiles_error,
        }
    )
    return templates.TemplateResponse("admin/network/onts/_mgmt_config.html", context)


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
    initial_form = _initial_iphost_form(ont, iphost_config)
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
    tr069 = tr069_data.get("tr069")

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.get("available"),
            "wan_info": tr069.get("wan") if tr069 else None,
            "current_pppoe_user": tr069.get("wan", {}).get("PPPoE Username") if tr069 else None,
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
    tr069 = tr069_data.get("tr069")

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.get("available"),
            "wireless_info": tr069.get("wireless") if tr069 else None,
            "current_ssid": tr069.get("wireless", {}).get("SSID") if tr069 else None,
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
    tr069 = tr069_data.get("tr069")

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.get("available"),
            "lan_info": tr069.get("lan") if tr069 else None,
            "ethernet_ports": tr069.get("ethernet_ports") if tr069 else None,
            "lan_hosts": tr069.get("lan_hosts") if tr069 else None,
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
    tr069 = tr069_data.get("tr069")

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "tr069_available": tr069 and tr069.get("available"),
        }
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_diagnostics.html", context
    )
