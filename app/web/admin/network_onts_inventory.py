"""Admin web routes for ONT inventory, detail, and edit flows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
from app.schemas.network import OntAssignmentUpdate
from app.services import network as network_service
from app.services import web_admin as web_admin_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.services.common import coerce_uuid
from app.services.credential_crypto import encrypt_credential
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-ont-inventory"])


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


def _authorization_result_from_request(request: Request) -> dict[str, object] | None:
    raw = request.query_params.get("authorize_result")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _ont_feedback_from_request(request: Request) -> dict[str, str] | None:
    status = request.query_params.get("feedback_status")
    message = request.query_params.get("feedback_message")
    if not status or not message:
        return None
    return {"status": str(status), "message": str(message)}


def _actor_id(request: Request) -> str | None:
    return web_admin_service.get_actor_id(request)


def _ont_redirect(
    ont_id: str,
    *,
    tab: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> RedirectResponse:
    params: list[str] = []
    if tab:
        params.append(f"tab={tab}")
    if status:
        params.append(f"feedback_status={status}")
    if message:
        params.append(f"feedback_message={quote_plus(message)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/admin/network/onts/{ont_id}{suffix}", status_code=303)


def _ont_form_dependencies(db: Session, ont: Any | None = None) -> dict:
    deps = web_network_onts_service.ont_form_dependencies(db, ont)
    deps["gpon_channels"] = [e.value for e in GponChannel]
    deps["onu_modes"] = [e.value for e in OnuMode]
    return deps


def _ont_has_active_assignment(db: Session, ont_id: str) -> bool:
    return web_network_ont_assignments_service.has_active_assignment(db, ont_id)


def _assignment_form_context(
    request: Request,
    db: Session,
    *,
    ont,
    action_url: str,
    form: dict[str, object] | None = None,
    error: str | None = None,
    mode: str = "create",
    assignment=None,
) -> dict[str, object]:
    deps = web_network_ont_assignments_service.assignment_form_dependencies(db)
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": ont,
            **deps,
            "action_url": action_url,
            "error": error,
            "form": form,
            "form_mode": mode,
            "assignment": assignment,
        }
    )
    return context


def _assignment_form_payload_from_assignment(assignment) -> dict[str, object]:
    subscriber = getattr(assignment, "subscriber", None)
    account_label = getattr(subscriber, "name", "") if subscriber else ""
    return {
        "pon_port_id": str(assignment.pon_port_id) if assignment.pon_port_id else "",
        "account_id": str(assignment.subscriber_id) if assignment.subscriber_id else "",
        "account_label": account_label,
        "subscription_id": (
            str(assignment.subscription_id) if assignment.subscription_id else ""
        ),
        "service_address_id": (
            str(assignment.service_address_id) if assignment.service_address_id else ""
        ),
        "notes": assignment.notes or "",
    }


def _form_uuid_or_none(form: FormData, key: str) -> str | None:
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    return raw.strip() or None


def _form_uuid_value(form: FormData, key: str):
    raw = _form_uuid_or_none(form, key)
    return coerce_uuid(raw) if raw else None


def _form_float_or_none(form: FormData, key: str) -> float | None:
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _ont_unit_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_ont_units_serial_number" in message:
        return "Serial number already exists"
    return "ONT could not be saved due to a data conflict"


def _normalize_vendor_serial(value: str) -> str | None:
    normalized = "".join(ch for ch in value.upper() if ch.isalnum()).strip()
    return normalized or None


@router.get(
    "/onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def onts_list(
    request: Request,
    view: str = "all",
    status: str | None = None,
    olt_id: str | None = None,
    pon_port_id: str | None = None,
    pon_hint: str | None = None,
    zone_id: str | None = None,
    online_status: str | None = None,
    offline_reason: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    pppoe_health: str | None = None,
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
        offline_reason=offline_reason,
        signal_quality=signal_quality,
        search=search,
        vendor=vendor,
        pppoe_health=pppoe_health,
        order_by=order_by,
        order_dir=order_dir,
        page=page,
    )
    context = _base_context(request, db, active_page="onts")
    context.update(page_data)
    context["firmware_images"] = web_network_onts_service.get_active_firmware_images(db)
    context["authorization_result"] = _authorization_result_from_request(request)
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
def ont_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": None,
            "action_url": "/admin/network/onts",
            **_ont_form_dependencies(db),
        }
    )
    return templates.TemplateResponse("admin/network/onts/form.html", context)


@router.post(
    "/onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_create(
    request: Request, db: Session = Depends(get_db)
):
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
        vendor_serial_number=_normalize_vendor_serial(
            _form_str(form, "vendor_serial_number").strip()
        ),
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
        onu_type_id=_form_uuid_value(form, "onu_type_id"),
        olt_device_id=_form_uuid_value(form, "olt_device_id"),
        pon_type=_form_str(form, "pon_type").strip() or None,
        gpon_channel=_form_str(form, "gpon_channel").strip() or None,
        board=_form_str(form, "board").strip() or None,
        port=_form_str(form, "port").strip() or None,
        onu_mode=_form_str(form, "onu_mode").strip() or None,
        user_vlan_id=_form_uuid_value(form, "user_vlan_id"),
        zone_id=_form_uuid_value(form, "zone_id"),
        splitter_id=_form_uuid_value(form, "splitter_id"),
        splitter_port_id=_form_uuid_value(form, "splitter_port_id"),
        download_speed_profile_id=_form_uuid_value(form, "download_speed_profile_id"),
        upload_speed_profile_id=_form_uuid_value(form, "upload_speed_profile_id"),
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
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ont",
            entity_id=str(ont.id),
            actor_id=_actor_id(request),
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
def ont_edit(request: Request, ont_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
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
    "/onts/{ont_id}/provision",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_provision_wizard(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Single-page ONT provisioning wizard."""
    from app.services import web_network_onts as web_network_onts_service

    context = web_network_onts_service.provision_wizard_context(request, db, ont_id)
    if context.get("error"):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": context["error"]},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/network/onts/provision.html", context
    )


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
        "device",
        "service",
        "support",
        # Legacy aliases for existing bookmarks/links
        "overview",
        "network",
        "history",
        "charts",
        "operations",
        "tr069",
        "service-ports",
        "provisioning",
    }
    active_tab = tab if tab in allowed_tabs else "device"
    legacy_tab_map = {
        "overview": "device",
        "network": "device",
        "operations": "service",
        "tr069": "service",
        "service-ports": "service",
        "provisioning": "service",
        "history": "support",
        "charts": "support",
    }
    active_tab = legacy_tab_map.get(active_tab, active_tab)

    # Only load tab-specific data when that tab is active
    activities: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    if active_tab == "support":
        activities = build_audit_activities(db, "ont", str(ont_id))
        try:
            operations = web_network_operations_service.build_operation_history(
                db, "ont", str(ont_id)
            )
        except Exception:
            import logging

            logging.getLogger(__name__).error(
                "Failed to load operation history for ONT %s",
                ont_id,
                exc_info=True,
            )

    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
            "now": datetime.now(UTC),
            "authorization_result": _authorization_result_from_request(request),
            "ont_feedback": _ont_feedback_from_request(request),
        }
    )
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get(
    "/onts/{ont_id}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_assign_new(request: Request, ont_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    context = _assignment_form_context(
        request,
        db,
        ont=ont,
        action_url=f"/admin/network/onts/{ont.id}/assign",
        mode="create",
    )
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.post(
    "/onts/{ont_id}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_assign_create(
    request: Request, ont_id: str, db: Session = Depends(get_db)
):
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
    resolved_pon_port_id = (
        coerce_uuid(str(values["pon_port_id"]))
        if values.get("pon_port_id")
        else getattr(ont, "pon_port_id", None)
    )
    if resolved_pon_port_id is None:
        error = error or "PON port is required"
    if not error and web_network_ont_assignments_service.has_active_assignment(
        db, ont_id
    ):
        error = "This ONT is already assigned"

    if error:
        context = _assignment_form_context(
            request,
            db,
            ont=ont,
            action_url=f"/admin/network/onts/{ont.id}/assign",
            error=error,
            form=web_network_ont_assignments_service.form_payload(values),
            mode="create",
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
        context = _assignment_form_context(
            request,
            db,
            ont=ont,
            action_url=f"/admin/network/onts/{ont.id}/assign",
            error=msg,
            form=web_network_ont_assignments_service.form_payload(values),
            mode="create",
        )
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    return _ont_redirect(
        str(ont.id),
        status="success",
        message="ONT assignment created.",
    )


@router.get(
    "/onts/{ont_id}/assignments/{assignment_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_assignment_edit(
    request: Request,
    ont_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    assignment = network_service.ont_assignments.get(db, assignment_id)
    if str(assignment.ont_unit_id) != str(ont.id):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Assignment not found for this ONT"},
            status_code=404,
        )

    context = _assignment_form_context(
        request,
        db,
        ont=ont,
        action_url=f"/admin/network/onts/{ont.id}/assignments/{assignment.id}/edit",
        form=_assignment_form_payload_from_assignment(assignment),
        mode="edit",
        assignment=assignment,
    )
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.post(
    "/onts/{ont_id}/assignments/{assignment_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_assignment_update(
    request: Request,
    ont_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
):
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    assignment = network_service.ont_assignments.get(db, assignment_id)
    if str(assignment.ont_unit_id) != str(ont.id):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Assignment not found for this ONT"},
            status_code=404,
        )

    values = web_network_ont_assignments_service.parse_form_values(
        parse_form_data_sync(request)
    )
    error = web_network_ont_assignments_service.validate_form_values(values)
    resolved_pon_port_id = (
        coerce_uuid(str(values["pon_port_id"]))
        if values.get("pon_port_id")
        else assignment.pon_port_id
    )
    if resolved_pon_port_id is None:
        error = error or "PON port is required"

    if error:
        context = _assignment_form_context(
            request,
            db,
            ont=ont,
            action_url=f"/admin/network/onts/{ont.id}/assignments/{assignment.id}/edit",
            error=error,
            form=web_network_ont_assignments_service.form_payload(values),
            mode="edit",
            assignment=assignment,
        )
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    payload = OntAssignmentUpdate(
        pon_port_id=resolved_pon_port_id,
        subscriber_id=coerce_uuid(str(values["account_id"])),
        subscription_id=(
            coerce_uuid(str(values["subscription_id"]))
            if values.get("subscription_id")
            else None
        ),
        service_address_id=(
            coerce_uuid(str(values["service_address_id"]))
            if values.get("service_address_id")
            else None
        ),
        notes=str(values.get("notes")) if values.get("notes") else None,
    )
    network_service.ont_assignments.update(db, assignment_id, payload)
    return _ont_redirect(
        str(ont.id),
        tab="operations",
        status="success",
        message="ONT assignment updated.",
    )


@router.post(
    "/onts/{ont_id}/assignments/{assignment_id}/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_assignment_remove(
    request: Request,
    ont_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
):
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    assignment = network_service.ont_assignments.get(db, assignment_id)
    if str(assignment.ont_unit_id) != str(ont.id):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Assignment not found for this ONT"},
            status_code=404,
        )

    network_service.ont_assignments.delete(db, assignment_id)
    return _ont_redirect(
        str(ont.id),
        tab="operations",
        status="success",
        message="ONT assignment removed.",
    )


@router.post(
    "/onts/{ont_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_update(
    request: Request, ont_id: str, db: Session = Depends(get_db)
):
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
        vendor_serial_number=_normalize_vendor_serial(
            _form_str(form, "vendor_serial_number").strip()
        ),
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
        onu_type_id=_form_uuid_value(form, "onu_type_id"),
        olt_device_id=_form_uuid_value(form, "olt_device_id"),
        pon_type=_form_str(form, "pon_type").strip() or None,
        gpon_channel=_form_str(form, "gpon_channel").strip() or None,
        board=_form_str(form, "board").strip() or None,
        port=_form_str(form, "port").strip() or None,
        onu_mode=_form_str(form, "onu_mode").strip() or None,
        user_vlan_id=_form_uuid_value(form, "user_vlan_id"),
        zone_id=_form_uuid_value(form, "zone_id"),
        splitter_id=_form_uuid_value(form, "splitter_id"),
        splitter_port_id=_form_uuid_value(form, "splitter_port_id"),
        download_speed_profile_id=_form_uuid_value(form, "download_speed_profile_id"),
        upload_speed_profile_id=_form_uuid_value(form, "upload_speed_profile_id"),
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
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="ont",
            entity_id=str(ont_id),
            actor_id=_actor_id(request),
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

    return _ont_redirect(
        str(ont.id),
        status="success",
        message="ONT details updated.",
    )


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
        wan_vlan_id=_form_uuid_value(form, "wan_vlan_id"),
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
    log_audit_event(
        db=db,
        request=request,
        action="update_onu_mode",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=_actor_id(request),
        metadata={"changes": changes} if changes else None,
    )
    return _ont_redirect(
        ont_id,
        status="success",
        message="ONU mode settings updated.",
    )


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
        mgmt_vlan_id=_form_uuid_value(form, "mgmt_vlan_id"),
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
    log_audit_event(
        db=db,
        request=request,
        action="update_mgmt_ip",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=_actor_id(request),
        metadata={"changes": changes} if changes else None,
    )
    return _ont_redirect(
        ont_id,
        status="success",
        message="Management IP settings updated.",
    )
