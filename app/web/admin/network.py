"""Admin network management web routes."""

from typing import cast

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db

# vendor_service removed during CRM cleanup
from app.models.network import (
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
)
from app.services import fiber_change_requests as change_request_service
from app.services import network as network_service
from app.services import radius as radius_service
from app.services import web_network_alarm_rules as web_network_alarm_rules_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services import web_network_fdh as web_network_fdh_service
from app.services import web_network_fiber as web_network_fiber_service
from app.services import web_network_ip as web_network_ip_service
from app.services import web_network_monitoring as web_network_monitoring_service
from app.services import web_network_olts as web_network_olts_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services import web_network_pop_sites as web_network_pop_sites_service
from app.services import web_network_radius as web_network_radius_service
from app.services import (
    web_network_splice_closures as web_network_splice_closures_service,
)
from app.services import web_network_strands as web_network_strands_service
from app.services import web_network_vlans as web_network_vlans_service
from app.services.audit_helpers import (
    build_audit_activities,
    build_audit_activities_for_types,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.web.request_parsing import parse_form_data_sync, parse_json_body_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

RADIUS_CLIENT_EXCLUDE_FIELDS = {"shared_secret_hash"}

# Re-export formatting/utility functions from the service layer for local use.
_coerce_float_or_none = web_network_core_runtime_service.coerce_float_or_none
_format_duration = web_network_core_runtime_service.format_duration
_format_bps = web_network_core_runtime_service.format_bps


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _form_optional_str(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


def _form_getlist_str(form: FormData, key: str) -> list[str]:
    return [value for value in form.getlist(key) if isinstance(value, str)]


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/olts", response_class=HTMLResponse)
def olts_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """List all OLT devices."""
    page_data = web_network_core_devices_service.olts_list_page_data(db)
    context = _base_context(request, db, active_page="olts")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/olts/index.html", context)


@router.get("/olts/new", response_class=HTMLResponse)
def olt_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="olts")
    context.update({
        "olt": None,
        "action_url": "/admin/network/olts",
    })
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post("/olts", response_class=HTMLResponse)
def olt_create(request: Request, db: Session = Depends(get_db)):
    values = web_network_olts_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_olts_service.validate_values(db, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": None,
            "action_url": "/admin/network/olts",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = web_network_olts_service.create_olt(db, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": web_network_olts_service.snapshot(values),
            "action_url": "/admin/network/olts",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="olt",
        entity_id=str(olt.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": olt.name, "mgmt_ip": olt.mgmt_ip or None},
    )

    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get("/olts/{olt_id}/edit", response_class=HTMLResponse)
def olt_edit(request: Request, olt_id: str, db: Session = Depends(get_db)):
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="olts")
    context.update({
        "olt": olt,
        "action_url": f"/admin/network/olts/{olt.id}",
    })
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post("/olts/{olt_id}", response_class=HTMLResponse)
def olt_update(request: Request, olt_id: str, db: Session = Depends(get_db)):
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    values = web_network_olts_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_olts_service.validate_values(db, values, current_olt=olt)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": olt,
            "action_url": f"/admin/network/olts/{olt.id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)

    before_snapshot = model_to_dict(olt)
    olt, error = web_network_olts_service.update_olt(db, olt_id, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": web_network_olts_service.snapshot(values),
            "action_url": f"/admin/network/olts/{olt_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    after = network_service.olt_devices.get(db=db, device_id=olt_id)
    after_snapshot = model_to_dict(after)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )

    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get("/olts/{olt_id}", response_class=HTMLResponse)
def olt_detail(request: Request, olt_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    page_data = web_network_core_devices_service.olt_detail_page_data(db, olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    activities = build_audit_activities(db, "olt", str(olt_id))
    context = _base_context(request, db, active_page="olts")
    context.update({**page_data, "activities": activities})
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.get("/onts", response_class=HTMLResponse)
def onts_list(request: Request, status: str | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    """List all ONT/CPE devices."""
    page_data = web_network_core_devices_service.onts_list_page_data(db, status)
    context = _base_context(request, db, active_page="onts")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/onts/index.html", context)


@router.get("/onts/new", response_class=HTMLResponse)
def ont_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": None,
        "action_url": "/admin/network/onts",
    })
    return templates.TemplateResponse("admin/network/onts/form.html", context)


def _ont_unit_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_ont_units_serial_number" in message:
        return "Serial number already exists"
    return "ONT could not be saved due to a data conflict"


@router.post("/onts", response_class=HTMLResponse)
def ont_create(request: Request, db: Session = Depends(get_db)):
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from app.schemas.network import OntUnitCreate

    form = parse_form_data_sync(request)
    serial_number = _form_str(form, "serial_number").strip()

    if not serial_number:
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": None,
            "action_url": "/admin/network/onts",
            "error": "Serial number is required",
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitCreate(
        serial_number=serial_number,
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
    )

    if payload.is_active:
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": payload,
            "action_url": "/admin/network/onts",
            "error": "New ONTs must be inactive until assigned to a customer.",
        })
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
        context.update({
            "ont": ont_snapshot,
            "action_url": "/admin/network/onts",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.get("/onts/{ont_id}/edit", response_class=HTMLResponse)
def ont_edit(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": ont,
        "action_url": f"/admin/network/onts/{ont.id}",
    })
    return templates.TemplateResponse("admin/network/onts/form.html", context)


@router.get("/onts/{ont_id}", response_class=HTMLResponse)
def ont_detail(request: Request, ont_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    page_data = web_network_core_devices_service.ont_detail_page_data(db, ont_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    activities = build_audit_activities(db, "ont", str(ont_id))
    context = _base_context(request, db, active_page="onts")
    context.update({**page_data, "activities": activities})
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get("/onts/{ont_id}/assign", response_class=HTMLResponse)
def ont_assign_new(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    deps = web_network_ont_assignments_service.assignment_form_dependencies(db)
    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": ont,
        **deps,
        "action_url": f"/admin/network/onts/{ont.id}/assign",
    })
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.post("/onts/{ont_id}/assign", response_class=HTMLResponse)
def ont_assign_create(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    values = web_network_ont_assignments_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_ont_assignments_service.validate_form_values(values)
    if not error and web_network_ont_assignments_service.has_active_assignment(db, ont_id):
        error = "This ONT is already assigned"

    if error:
        deps = web_network_ont_assignments_service.assignment_form_dependencies(db)
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": ont,
            **deps,
            "action_url": f"/admin/network/onts/{ont.id}/assign",
            "error": error,
            "form": web_network_ont_assignments_service.form_payload(values),
        })
        return templates.TemplateResponse("admin/network/onts/assign.html", context)
    web_network_ont_assignments_service.create_assignment(db, ont, values)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.post("/onts/{ont_id}", response_class=HTMLResponse)
def ont_update(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from app.schemas.network import OntUnitUpdate

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    serial_number = _form_str(form, "serial_number").strip()

    if not serial_number:
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": ont,
            "action_url": f"/admin/network/onts/{ont.id}",
            "error": "Serial number is required",
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitUpdate(
        serial_number=serial_number,
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
    )

    try:
        before_snapshot = model_to_dict(ont)
        ont = network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
        after = network_service.ont_units.get(db=db, unit_id=ont_id)
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
        context.update({
            "ont": ont_snapshot,
            "action_url": f"/admin/network/onts/{ont_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.get("/cpes/{cpe_id}", response_class=HTMLResponse)
def cpe_detail(request: Request, cpe_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    try:
        cpe = network_service.cpe_devices.get(db=db, device_id=cpe_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )

    ports = web_network_core_devices_service.get_cpe_ports(db, cpe.id)
    activities = build_audit_activities(db, "cpe", str(cpe_id))
    context = _base_context(request, db, active_page="onts")
    context.update({
        "cpe": cpe,
        "ports": ports,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/cpes/detail.html", context)


@router.get("/devices", response_class=HTMLResponse)
def devices_list(
    request: Request,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all network devices."""
    page_data = web_network_core_devices_service.devices_list_page_data(
        db, device_type=device_type, search=search, status=status, vendor=vendor
    )
    context = _base_context(request, db, active_page="devices")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/devices/index.html", context)


@router.get("/devices/search", response_class=HTMLResponse)
def devices_search(request: Request, search: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    devices = web_network_core_devices_service.devices_search_data(db, search)
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get("/devices/filter", response_class=HTMLResponse)
def devices_filter(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    devices = web_network_core_devices_service.devices_filter_data(
        db, search=search, status=status, vendor=vendor
    )
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.post("/devices/discover", response_class=HTMLResponse)
def devices_discover(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        "Discovery queued. Devices will appear as they are detected."
        "</div>"
    )


@router.get("/devices/create", response_class=HTMLResponse)
def device_create(request: Request, db: Session = Depends(get_db)):
    # Redirect to the more specific device creation pages
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/network/core-devices/new", status_code=302)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: str, db: Session = Depends(get_db)) -> Response:
    redirect_url = web_network_core_devices_service.resolve_device_redirect(db, device_id)
    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=302)

    return templates.TemplateResponse(
        "admin/errors/404.html",
        {"request": request, "message": "Device not found"},
        status_code=404,
    )


@router.post("/devices/{device_id}/ping", response_class=HTMLResponse)
def device_ping(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Ping queued for device {device_id}."
        "</div>"
    )


@router.post("/devices/{device_id}/reboot", response_class=HTMLResponse)
def device_reboot(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Reboot request queued for device {device_id}."
        "</div>"
    )


@router.get("/ip-management", response_class=HTMLResponse)
def ip_management(request: Request, db: Session = Depends(get_db)):
    """IP address management page - consolidated view with tabs."""
    state = web_network_ip_service.build_ip_management_data(db)

    context = _base_context(request, db, active_page="ip-management", active_menu="network")
    context.update(
        {
            **state,
            "activities": build_audit_activities_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/index.html", context)


@router.get("/ip-management/pools/new", response_class=HTMLResponse)
def ip_pool_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update(web_network_ip_service.get_ip_pool_new_form_data())
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/pools", response_class=HTMLResponse)
def ip_pools_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.get("/ip-management/blocks/new", response_class=HTMLResponse)
def ip_block_new(
    request: Request,
    pool_id: str | None = None,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update(web_network_ip_service.get_ip_block_new_form_data(db, pool_id=pool_id))
    return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)


@router.get("/ip-management/blocks", response_class=HTMLResponse)
def ip_blocks_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.post("/ip-management/blocks", response_class=HTMLResponse)
def ip_block_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    block_data = web_network_ip_service.parse_ip_block_form(form)
    error = web_network_ip_service.validate_ip_block_values(block_data)

    if error:
        context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
        context.update(
            {
                "block": block_data,
                "pools": web_network_ip_service.list_active_ip_pools(db),
                "action_url": "/admin/network/ip-management/blocks",
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)

    block, error = web_network_ip_service.create_ip_block(db, block_data)
    if not error and block is not None:
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ip_block",
            entity_id=str(block.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"cidr": block.cidr, "pool_id": str(block.pool_id)},
        )
        return RedirectResponse("/admin/network/ip-management", status_code=303)

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update(
        {
            "block": block_data,
            "pools": web_network_ip_service.list_active_ip_pools(db),
            "action_url": "/admin/network/ip-management/blocks",
            "error": error or "Please correct the highlighted fields.",
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)


@router.post("/ip-management/pools", response_class=HTMLResponse)
def ip_pool_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    pool_values = web_network_ip_service.parse_ip_pool_form(form)
    error = web_network_ip_service.validate_ip_pool_values(pool_values)
    pool_data = web_network_ip_service.pool_form_snapshot(pool_values)

    if error:
        context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
        context.update({
            "pool": pool_data,
            "action_url": "/admin/network/ip-management/pools",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)

    pool, error = web_network_ip_service.create_ip_pool(db, pool_values)
    if not error and pool is not None:
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ip_pool",
            entity_id=str(pool.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": pool.name, "cidr": pool.cidr},
        )
        return RedirectResponse("/admin/network/ip-management", status_code=303)

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool_data,
        "action_url": "/admin/network/ip-management/pools",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/pools/{pool_id}", response_class=HTMLResponse)
def ip_pool_detail(request: Request, pool_id: str, db: Session = Depends(get_db)):
    state = web_network_ip_service.build_ip_pool_detail_data(db, pool_id=pool_id)
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )
    pool = state["pool"]
    activities = build_audit_activities(db, "ip_pool", str(pool_id))
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({**state, "activities": activities})
    return templates.TemplateResponse("admin/network/ip-management/pool_detail.html", context)


@router.get("/ip-management/pools/{pool_id}/edit", response_class=HTMLResponse)
def ip_pool_edit(request: Request, pool_id: str, db: Session = Depends(get_db)):
    pool = web_network_ip_service.get_ip_pool_for_edit(db, pool_id=pool_id)
    if pool is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool,
        "action_url": f"/admin/network/ip-management/pools/{pool_id}",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.post("/ip-management/pools/{pool_id}", response_class=HTMLResponse)
def ip_pool_update(request: Request, pool_id: str, db: Session = Depends(get_db)):
    pool = web_network_ip_service.get_ip_pool_for_edit(db, pool_id=pool_id)
    if pool is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    pool_values = web_network_ip_service.parse_ip_pool_form(form)
    error = web_network_ip_service.validate_ip_pool_values(pool_values)
    pool_data = web_network_ip_service.pool_form_snapshot(pool_values, pool_id=str(pool.id))

    if error:
        context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
        context.update({
            "pool": pool_data,
            "action_url": f"/admin/network/ip-management/pools/{pool_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)

    _, changes, error = web_network_ip_service.update_ip_pool(
        db,
        pool_id=pool_id,
        values=pool_values,
    )
    if not error:
        metadata_payload = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="ip_pool",
            entity_id=str(pool_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(f"/admin/network/ip-management/pools/{pool_id}", status_code=303)

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool_data,
        "action_url": f"/admin/network/ip-management/pools/{pool_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/calculator", response_class=HTMLResponse)
def ip_calculator(request: Request, db: Session = Depends(get_db)):
    """IP subnet calculator tool."""
    context = _base_context(request, db, active_page="ip-calculator", active_menu="ip-address")
    return templates.TemplateResponse("admin/network/ip-management/calculator.html", context)


@router.get("/ip-management/assignments", response_class=HTMLResponse)
def ip_assignments_list(request: Request, db: Session = Depends(get_db)):
    """List all IP assignments."""
    state = web_network_ip_service.build_ip_assignments_data(db)

    context = _base_context(request, db, active_page="ip-assignments", active_menu="ip-address")
    context.update(
        {
            **state,
            "activities": build_audit_activities_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/assignments.html", context)


@router.get("/ip-management/ipv4", response_class=HTMLResponse)
def ipv4_addresses_list(request: Request, db: Session = Depends(get_db)):
    """List all IPv4 addresses."""
    state = web_network_ip_service.build_ip_addresses_data(db, ip_version="ipv4")

    context = _base_context(request, db, active_page="ipv4-addresses", active_menu="ip-address")
    context.update(
        {
            **state,
            "activities": build_audit_activities_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/addresses.html", context)


@router.get("/ip-management/ipv6", response_class=HTMLResponse)
def ipv6_addresses_list(request: Request, db: Session = Depends(get_db)):
    """List all IPv6 addresses."""
    state = web_network_ip_service.build_ip_addresses_data(db, ip_version="ipv6")

    context = _base_context(request, db, active_page="ipv6-addresses", active_menu="ip-address")
    context.update(
        {
            **state,
            "activities": build_audit_activities_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/addresses.html", context)


@router.get("/ip-management/pools", response_class=HTMLResponse)
def ip_pools_list(request: Request, db: Session = Depends(get_db)):
    """List all IP pools and blocks."""
    state = web_network_ip_service.build_ip_pools_data(db)

    context = _base_context(request, db, active_page="ip-pools", active_menu="ip-address")
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/pools.html", context)


@router.get("/vlans", response_class=HTMLResponse)
def vlans_list(request: Request, db: Session = Depends(get_db)):
    """List all VLANs."""
    state = web_network_vlans_service.build_vlans_list_data(db)

    context = _base_context(request, db, active_page="vlans")
    context.update(state)
    return templates.TemplateResponse("admin/network/vlans/index.html", context)


@router.get("/vlans/new", response_class=HTMLResponse)
def vlan_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="vlans")
    context.update(web_network_vlans_service.build_vlan_new_form_data(db))
    return templates.TemplateResponse("admin/network/vlans/form.html", context)


@router.get("/vlans/{vlan_id}", response_class=HTMLResponse)
def vlan_detail(request: Request, vlan_id: str, db: Session = Depends(get_db)):
    state = web_network_vlans_service.build_vlan_detail_data(db, vlan_id=vlan_id)
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "VLAN not found"},
            status_code=404,
        )

    activities = build_audit_activities(db, "vlan", str(vlan_id))
    context = _base_context(request, db, active_page="vlans")
    context.update({**state, "activities": activities})
    return templates.TemplateResponse("admin/network/vlans/detail.html", context)


@router.get("/radius", response_class=HTMLResponse)
def radius_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    state = web_network_radius_service.radius_page_data(db)
    activities = build_audit_activities_for_types(
        db,
        ["radius_server", "radius_client", "radius_profile"],
        limit=10,
    )
    context.update({**state, "activities": activities})
    return templates.TemplateResponse("admin/network/radius/index.html", context)


@router.get("/radius/servers/new", response_class=HTMLResponse)
def radius_server_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": None,
        "action_url": "/admin/network/radius/servers",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get("/radius/servers", response_class=HTMLResponse)
def radius_servers_redirect():
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.get("/radius/servers/{server_id}/edit", response_class=HTMLResponse)
def radius_server_edit(request: Request, server_id: str, db: Session = Depends(get_db)):
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server,
        "action_url": f"/admin/network/radius/servers/{server_id}",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post("/radius/servers", response_class=HTMLResponse)
def radius_server_create(request: Request, db: Session = Depends(get_db)):
    values = web_network_radius_service.parse_server_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_server_form(values)
    payload = None
    if not error:
        payload, error = web_network_radius_service.build_server_create_payload(values)
    server_data = web_network_radius_service.server_create_form_data(values)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "server": server_data,
            "action_url": "/admin/network/radius/servers",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/server_form.html", context)

    try:
        assert payload is not None
        server = radius_service.radius_servers.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="radius_server",
            entity_id=str(server.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": server.name, "host": server.host},
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server_data,
        "action_url": "/admin/network/radius/servers",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post("/radius/servers/{server_id}", response_class=HTMLResponse)
def radius_server_update(request: Request, server_id: str, db: Session = Depends(get_db)):
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(server)
    values = web_network_radius_service.parse_server_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_server_form(values)
    payload = None
    if not error:
        payload, error = web_network_radius_service.build_server_payload(
            values,
            current_server=server,
        )
    server_data = web_network_radius_service.server_form_data(
        values,
        current_server=server,
    )

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "server": server_data,
            "action_url": f"/admin/network/radius/servers/{server_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/server_form.html", context)

    try:
        assert payload is not None
        updated_server = radius_service.radius_servers.update(
            db=db,
            server_id=server_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_server)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_server",
            entity_id=str(updated_server.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server_data,
        "action_url": f"/admin/network/radius/servers/{server_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get("/radius/clients/new", response_class=HTMLResponse)
def radius_client_new(request: Request, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": None,
        "servers": servers,
        "action_url": "/admin/network/radius/clients",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get("/radius/clients/{client_id}/edit", response_class=HTMLResponse)
def radius_client_edit(request: Request, client_id: str, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)

    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client,
        "servers": servers,
        "action_url": f"/admin/network/radius/clients/{client_id}",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post("/radius/clients", response_class=HTMLResponse)
def radius_client_create(request: Request, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)
    values = web_network_radius_service.parse_client_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_client_form(values, require_secret=True)
    client_data = web_network_radius_service.client_form_data(values)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "client": client_data,
            "servers": servers,
            "action_url": "/admin/network/radius/clients",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/client_form.html", context)

    try:
        payload = web_network_radius_service.build_client_create_payload(values)
        client = radius_service.radius_clients.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="radius_client",
            entity_id=str(client.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"client_ip": client.client_ip, "server_id": str(client.server_id)},
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client_data,
        "servers": servers,
        "action_url": "/admin/network/radius/clients",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post("/radius/clients/{client_id}", response_class=HTMLResponse)
def radius_client_update(request: Request, client_id: str, db: Session = Depends(get_db)):
    servers = web_network_radius_service.active_servers(db)

    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
    values = web_network_radius_service.parse_client_form(parse_form_data_sync(request))
    error = web_network_radius_service.validate_client_form(values, require_secret=False)
    client_data = web_network_radius_service.client_form_data(
        values,
        client_id=str(client.id),
    )

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "client": client_data,
            "servers": servers,
            "action_url": f"/admin/network/radius/clients/{client_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/client_form.html", context)

    try:
        payload = web_network_radius_service.build_client_update_payload(values)
        updated_client = radius_service.radius_clients.update(
            db=db,
            client_id=client_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_client",
            entity_id=str(updated_client.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client_data,
        "servers": servers,
        "action_url": f"/admin/network/radius/clients/{client_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get("/radius/profiles/new", response_class=HTMLResponse)
def radius_profile_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update(web_network_radius_service.profile_new_form_data())
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.get("/radius/profiles/{profile_id}/edit", response_class=HTMLResponse)
def radius_profile_edit(request: Request, profile_id: str, db: Session = Depends(get_db)):
    form_data = web_network_radius_service.profile_edit_form_data(db, profile_id)
    if not form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update(form_data)
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.post("/radius/profiles", response_class=HTMLResponse)
def radius_profile_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    profile_data, attributes, error = web_network_radius_service.parse_profile_form(form)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "profile": profile_data,
            "attributes": attributes,
            "vendors": web_network_radius_service.profile_vendors(),
            "action_url": "/admin/network/radius/profiles",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/profile_form.html", context)

    profile, metadata = web_network_radius_service.create_profile(db, profile_data, attributes)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="radius_profile",
        entity_id=str(profile.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.post("/radius/profiles/{profile_id}", response_class=HTMLResponse)
def radius_profile_update(request: Request, profile_id: str, db: Session = Depends(get_db)):
    profile_form_data = web_network_radius_service.profile_edit_form_data(db, profile_id)
    if not profile_form_data:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    form = parse_form_data_sync(request)
    profile_data, attributes, error = web_network_radius_service.parse_profile_form(form)

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "profile": profile_data,
            "attributes": attributes,
            "vendors": web_network_radius_service.profile_vendors(),
            "action_url": f"/admin/network/radius/profiles/{profile_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/profile_form.html", context)

    updated_profile, metadata = web_network_radius_service.update_profile(
        db=db,
        profile_id=profile_id,
        profile_data=profile_data,
        attributes=attributes,
    )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="radius_profile",
        entity_id=str(updated_profile.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.get("/monitoring", response_class=HTMLResponse)
def monitoring_page(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_monitoring_service.monitoring_page_data(
        db,
        format_duration=_format_duration,
        format_bps=_format_bps,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    context["activities"] = build_audit_activities_for_types(
        db,
        ["core_device", "network_device"],
        limit=5,
    )
    return templates.TemplateResponse("admin/network/monitoring/index.html", context)


@router.get("/alarms", response_class=HTMLResponse)
def alarms_page(
    request: Request,
    severity: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_monitoring_service.alarms_page_data(
        db,
        severity=severity,
        status=status,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/monitoring/alarms.html", context)


@router.get("/alarms/rules/new", response_class=HTMLResponse)
def alarms_rules_new(request: Request, db: Session = Depends(get_db)):
    options = web_network_alarm_rules_service.form_options(db)
    context = _base_context(request, db, active_page="monitoring")
    context.update(
        {
            "rule": None,
            "action_url": "/admin/network/alarms/rules/new",
            **options,
        }
    )
    return templates.TemplateResponse("admin/network/monitoring/rule_form.html", context)


@router.post("/alarms/rules/new", response_class=HTMLResponse)
def alarms_rules_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_alarm_rules_service.parse_form_values(form)
    normalized, error = web_network_alarm_rules_service.validate_form_values(values)
    if not error:
        assert normalized is not None
        error = web_network_alarm_rules_service.create_rule(db, normalized)
        if not error:
            return RedirectResponse(url="/admin/network/alarms", status_code=303)

    options = web_network_alarm_rules_service.form_options(db)
    rule = web_network_alarm_rules_service.rule_form_data(values)
    context = _base_context(request, db, active_page="monitoring")
    context.update(
        {
            "rule": rule,
            "action_url": "/admin/network/alarms/rules/new",
            **options,
            "error": error or "Please correct the highlighted fields.",
        }
    )
    return templates.TemplateResponse("admin/network/monitoring/rule_form.html", context)


# ==================== POP Sites ====================

@router.get("/pop-sites", response_class=HTMLResponse)
def pop_sites_list(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    """List all POP sites."""
    page_data = web_network_pop_sites_service.list_page_data(db, status)
    context = _base_context(request, db, active_page="pop-sites")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/pop-sites/index.html", context)


@router.get("/pop-sites/new", response_class=HTMLResponse)
def pop_site_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_pop_sites_service.build_form_context(
        pop_site=None,
        action_url="/admin/network/pop-sites",
        mast_enabled=False,
        mast_defaults=web_network_pop_sites_service.default_mast_context(),
    )
    context = _base_context(request, db, active_page="pop-sites")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/pop-sites/form.html", context)


@router.post("/pop-sites", response_class=HTMLResponse)
def pop_site_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_pop_sites_service.parse_site_form_values(form)
    normalized, error = web_network_pop_sites_service.validate_site_values(values)
    lat_value = _coerce_float_or_none(normalized.get("latitude")) if normalized else None
    lon_value = _coerce_float_or_none(normalized.get("longitude")) if normalized else None
    mast_enabled, mast_data, mast_error, mast_defaults = web_network_pop_sites_service.parse_mast_form(
        form, lat_value, lon_value
    )

    if error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=None,
            action_url="/admin/network/pop-sites",
            error=error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)
    if mast_error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=None,
            action_url="/admin/network/pop-sites",
            mast_error=mast_error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    assert normalized is not None
    pop_site = web_network_pop_sites_service.create_site(db, normalized)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="pop_site",
        entity_id=str(pop_site.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": pop_site.name, "code": pop_site.code},
    )

    if mast_enabled:
        web_network_pop_sites_service.maybe_create_mast(db, str(pop_site.id), mast_data)

    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get("/pop-sites/{pop_site_id}/edit", response_class=HTMLResponse)
def pop_site_edit(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )
    form_context = web_network_pop_sites_service.build_form_context(
        pop_site=pop_site,
        action_url=f"/admin/network/pop-sites/{pop_site.id}",
        mast_enabled=False,
        mast_defaults=web_network_pop_sites_service.default_mast_context(),
    )
    context = _base_context(request, db, active_page="pop-sites")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/pop-sites/form.html", context)


@router.post("/pop-sites/{pop_site_id}", response_class=HTMLResponse)
def pop_site_update(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    pop_site = web_network_pop_sites_service.get_pop_site(db, pop_site_id)
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    values = web_network_pop_sites_service.parse_site_form_values(form)
    normalized, error = web_network_pop_sites_service.validate_site_values(values)
    fallback_lat = (
        _coerce_float_or_none(normalized.get("latitude")) if normalized else pop_site.latitude
    )
    fallback_lon = (
        _coerce_float_or_none(normalized.get("longitude")) if normalized else pop_site.longitude
    )
    mast_enabled, mast_data, mast_error, mast_defaults = web_network_pop_sites_service.parse_mast_form(
        form, fallback_lat, fallback_lon
    )

    if error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=pop_site,
            action_url=f"/admin/network/pop-sites/{pop_site.id}",
            error=error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    assert normalized is not None
    if mast_error:
        form_context = web_network_pop_sites_service.build_form_context(
            pop_site=pop_site,
            action_url=f"/admin/network/pop-sites/{pop_site.id}",
            mast_error=mast_error,
            mast_enabled=mast_enabled,
            mast_defaults=mast_defaults,
        )
        context = _base_context(request, db, active_page="pop-sites")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    before_snapshot = model_to_dict(pop_site)
    web_network_pop_sites_service.commit_site_update(db, pop_site, normalized)
    after_snapshot = model_to_dict(pop_site)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="pop_site",
        entity_id=str(pop_site.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )

    if mast_enabled:
        web_network_pop_sites_service.maybe_create_mast(db, str(pop_site.id), mast_data)

    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get("/pop-sites/{pop_site_id}", response_class=HTMLResponse)
def pop_site_detail(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    page_data = web_network_pop_sites_service.detail_page_data(db, pop_site_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    activities = build_audit_activities(db, "pop_site", str(pop_site_id))
    context = _base_context(request, db, active_page="pop-sites")
    context.update(page_data)
    context["activities"] = activities
    return templates.TemplateResponse("admin/network/pop-sites/detail.html", context)


# ==================== Network Devices (Consolidated) ====================

@router.get("/network-devices", response_class=HTMLResponse)
def network_devices_consolidated(
    request: Request,
    tab: str = "core",
    db: Session = Depends(get_db),
):
    """Consolidated view of all network devices - core, OLTs, ONTs/CPE."""
    page_data = web_network_core_devices_service.consolidated_page_data(tab, db)
    context = _base_context(request, db, active_page="network-devices")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/network-devices/index.html", context)


# ==================== Core Network Devices ====================

@router.get("/core-devices", response_class=HTMLResponse)
def core_devices_list(
    request: Request,
    role: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List core network devices (routers, switches, access points, etc.)."""
    page_data = web_network_core_devices_service.list_page_data(db, role, status)
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/index.html", context)


@router.get("/core-devices/new", response_class=HTMLResponse)
def core_device_new(request: Request, db: Session = Depends(get_db)):
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    form_context = web_network_core_devices_service.build_form_context(
        device=None,
        pop_sites=pop_sites,
        action_url="/admin/network/core-devices",
    )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.post("/core-devices", response_class=HTMLResponse)
def core_device_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    values = web_network_core_devices_service.parse_form_values(form)
    normalized, error = web_network_core_devices_service.validate_values(db, values)
    if error:
        snapshot = web_network_core_devices_service.snapshot_for_form(values)
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            action_url="/admin/network/core-devices",
            error=error,
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    assert normalized is not None
    result = web_network_core_devices_service.create_device(db, normalized)
    if result.error:
        form_context = web_network_core_devices_service.build_form_context(
            device=result.snapshot,
            pop_sites=pop_sites,
            action_url="/admin/network/core-devices",
            error=result.error,
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)
    device = result.device
    if device is None:
        form_context = web_network_core_devices_service.build_form_context(
            device=result.snapshot,
            pop_sites=pop_sites,
            action_url="/admin/network/core-devices",
            error="Failed to create device",
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="core_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": device.name, "mgmt_ip": device.mgmt_ip or None},
    )
    return RedirectResponse(f"/admin/network/core-devices/{device.id}", status_code=303)


@router.get("/core-devices/{device_id}/edit", response_class=HTMLResponse)
def core_device_edit(request: Request, device_id: str, db: Session = Depends(get_db)):
    device = web_network_core_devices_service.get_device(db, device_id)
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    form_context = web_network_core_devices_service.build_form_context(
        device=device,
        pop_sites=pop_sites,
        action_url=f"/admin/network/core-devices/{device.id}",
    )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.get("/core-devices/{device_id}", response_class=HTMLResponse)
def core_device_detail(request: Request, device_id: str, db: Session = Depends(get_db)):
    page_data = web_network_core_devices_service.detail_page_data(
        db,
        device_id,
        request.query_params.get("interface_id"),
        format_duration=_format_duration,
        format_bps=_format_bps,
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    activities = build_audit_activities(db, "core_device", str(device_id))
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    context["activities"] = activities
    return templates.TemplateResponse("admin/network/core-devices/detail.html", context)


@router.post("/core-devices/{device_id}/ping", response_class=HTMLResponse)
def core_device_ping(request: Request, device_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    device, error, ping_success = web_network_core_runtime_service.ping_device(db, device_id)
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    if error:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            f'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">{error}</div>'
        )

    status_label = "reachable" if ping_success else "unreachable"
    message = (
        '<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 '
        'dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400">'
        f"Ping successful: device is {status_label}.</div>"
        if ping_success
        else
        '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
        'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">'
        f"Ping failed: device is {status_label}.</div>"
    )

    badge = web_network_core_runtime_service.render_device_status_badge(device.status.value)
    ping_badge = web_network_core_runtime_service.render_ping_badge(device)
    return HTMLResponse(
        message
        + f'<div id="device-status-badge" hx-swap-oob="true">{badge}</div>'
        + f'<span id="device-ping-badge" hx-swap-oob="true">{ping_badge}</span>'
    )


@router.post("/core-devices/{device_id}/snmp-check", response_class=HTMLResponse)
def core_device_snmp_check(request: Request, device_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    device, error = web_network_core_runtime_service.snmp_check_device(db, device_id)
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    snmp_badge = web_network_core_runtime_service.render_snmp_badge(device)
    return HTMLResponse(
        f'<span id="device-snmp-badge" hx-swap-oob="true">{snmp_badge}</span>'
    )


@router.post("/core-devices/{device_id}/snmp-debug", response_class=HTMLResponse)
def core_device_snmp_debug(request: Request, device_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    result = web_network_core_runtime_service.snmp_debug_device(db, device_id)
    if result.error:
        css = (
            "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-400"
            if "not found" in result.error or "failed" in result.error.lower()
            else "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400"
        )
        return HTMLResponse(
            f'<div class="rounded-lg border {css} px-4 py-3 text-sm">{result.error}</div>',
            status_code=404 if "not found" in result.error else 200,
        )

    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white p-4 text-xs text-slate-700 '
        'dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200">'
        f'<pre class="whitespace-pre-wrap">{result.output}</pre>'
        "</div>"
    )


@router.get("/core-devices/{device_id}/health", response_class=HTMLResponse)
def core_device_health_partial(request: Request, device_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    device = web_network_core_runtime_service.get_device(db, device_id)
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )
    device_health = web_network_core_runtime_service.compute_health(
        db,
        device,
        interface_id=request.query_params.get("interface_id"),
        format_duration=_format_duration,
        format_bps=_format_bps,
    )

    html = web_network_core_runtime_service.render_device_health_content(device_health)
    return HTMLResponse(
        f'<div id="device-health-content" hx-swap-oob="true">{html}</div>'
    )


@router.post("/core-devices/{device_id}/discover-interfaces", response_class=HTMLResponse)
def core_device_discover_interfaces(request: Request, device_id: str, db: Session = Depends(get_db)):
    device = web_network_core_runtime_service.get_device(db, device_id)
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    if not device.snmp_enabled:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            "SNMP is disabled for this device."
            "</div>"
        )

    if not device.mgmt_ip and not device.hostname:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            "Management IP or hostname is required for SNMP discovery."
            "</div>"
        )

    try:
        created, updated = web_network_core_runtime_service.discover_interfaces_and_health(
            db, device
        )
    except Exception as exc:
        web_network_core_runtime_service.mark_discovery_failure(db, device)
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">'
            f"SNMP discovery failed: {exc!s}"
            "</div>"
        )

    refresh = request.query_params.get("refresh", "true").lower() != "false"
    headers = {}
    if refresh:
        headers["HX-Refresh"] = "true"
    else:
        headers["HX-Trigger"] = "snmp-discovered"
    return HTMLResponse(
        '<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 '
        'dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400">'
        f"Discovery complete: {created} new, {updated} updated interfaces."
        "</div>",
        headers=headers,
    )


@router.post("/core-devices/{device_id}", response_class=HTMLResponse)
def core_device_update(request: Request, device_id: str, db: Session = Depends(get_db)):
    device = web_network_core_devices_service.get_device(db, device_id)
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    before_snapshot = model_to_dict(device)

    form = parse_form_data_sync(request)
    values = web_network_core_devices_service.parse_form_values(form)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    normalized, error = web_network_core_devices_service.validate_values(
        db,
        values,
        current_device=device,
    )
    if error:
        snapshot = web_network_core_devices_service.snapshot_for_form(
            values,
            device_id=str(device.id),
            status=device.status,
        )
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            action_url=f"/admin/network/core-devices/{device.id}",
            error=error,
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    assert normalized is not None
    result = web_network_core_devices_service.update_device(db, device, normalized)
    if result.error:
        form_context = web_network_core_devices_service.build_form_context(
            device=result.snapshot,
            pop_sites=pop_sites,
            action_url=f"/admin/network/core-devices/{device.id}",
            error=result.error,
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    after_snapshot = model_to_dict(device)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="core_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )
    return RedirectResponse(f"/admin/network/core-devices/{device.id}", status_code=303)


# ==================== Fiber Plant (ODN) ====================

@router.get("/fiber-plant", response_class=HTMLResponse)
def fiber_plant_consolidated(
    request: Request,
    tab: str = "cabinets",
    db: Session = Depends(get_db),
):
    """Consolidated view of fiber plant infrastructure."""
    page_data = web_network_fiber_service.get_fiber_plant_consolidated_data(db)
    context = _base_context(request, db, active_page="fiber-plant", active_menu="fiber")
    context.update({"tab": tab, **page_data})
    return templates.TemplateResponse("admin/network/fiber-plant/index.html", context)


@router.get("/fiber-map", response_class=HTMLResponse)
def fiber_plant_map(request: Request, db: Session = Depends(get_db)):
    """Interactive fiber plant map."""
    page_data = web_network_fiber_service.get_fiber_plant_map_data(db)
    context = _base_context(request, db, active_page="fiber-map", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/map.html", context)


@router.get("/fiber-change-requests", response_class=HTMLResponse)
def fiber_change_requests(request: Request, db: Session = Depends(get_db)):
    """Review pending vendor fiber change requests."""
    from app.models.fiber_change_request import FiberChangeRequestStatus

    requests = change_request_service.list_requests(
        db, status=FiberChangeRequestStatus.pending
    )
    conflicts = {
        str(req.id): web_network_fiber_service.has_change_request_conflict(db, req)
        for req in requests
    }
    bulk_status = request.query_params.get("bulk")
    skipped = request.query_params.get("skipped")
    context = _base_context(request, db, active_page="fiber-change-requests", active_menu="fiber")
    context.update(
        {
            "requests": requests,
            "conflicts": conflicts,
            "bulk_status": bulk_status,
            "skipped": skipped,
        }
    )
    return templates.TemplateResponse(
        "admin/network/fiber/change_requests.html", context
    )


@router.get("/fiber-change-requests/{request_id}", response_class=HTMLResponse)
def fiber_change_request_detail(request: Request, request_id: str, db: Session = Depends(get_db)):
    """Review a specific fiber change request."""
    from app.models.fiber_change_request import FiberChangeRequestStatus
    from app.services import fiber_change_requests as change_requests

    change_request = change_requests.get_request(db, request_id)
    asset_data: dict[str, object] = {}
    conflict = web_network_fiber_service.has_change_request_conflict(db, change_request)
    if change_request.asset_id:
        asset = web_network_core_devices_service.get_change_request_asset(
            db, change_request.asset_type, str(change_request.asset_id)
        )
        asset_data = web_network_fiber_service.serialize_asset(asset)

    context = _base_context(request, db, active_page="fiber-change-requests", active_menu="fiber")
    context.update(
        {
            "change_request": change_request,
            "asset_data": asset_data,
            "conflict": conflict,
            "pending": change_request.status == FiberChangeRequestStatus.pending,
            "error": request.query_params.get("error"),
            "activities": build_audit_activities(
                db,
                "fiber_change_request",
                str(request_id),
                limit=10,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/fiber/change_request_detail.html", context
    )


@router.post("/fiber-change-requests/{request_id}/approve")
def fiber_change_request_approve(request: Request, request_id: str, db: Session = Depends(get_db)):
    data = parse_form_data_sync(request)
    review_notes = _form_optional_str(data, "review_notes")
    force_apply = data.get("force_apply") == "true"
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    change_request = change_request_service.get_request(db, request_id)
    if web_network_fiber_service.has_change_request_conflict(db, change_request) and not force_apply:
        return RedirectResponse(
            url=f"/admin/network/fiber-change-requests/{request_id}?error=conflict",
            status_code=303,
        )
    change_request_service.approve_request(
        db, request_id, reviewer_person_id=current_user["person_id"], review_notes=review_notes
    )
    log_audit_event(
        db=db,
        request=request,
        action="approve",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"force_apply": force_apply, "review_notes": review_notes},
    )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests/{request_id}", status_code=303
    )


@router.post("/fiber-change-requests/{request_id}/reject")
def fiber_change_request_reject(request: Request, request_id: str, db: Session = Depends(get_db)):
    data = parse_form_data_sync(request)
    review_notes = _form_optional_str(data, "review_notes")
    if not review_notes or not str(review_notes).strip():
        return RedirectResponse(
            url=f"/admin/network/fiber-change-requests/{request_id}?error=reject_note_required",
            status_code=303,
        )
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    change_request_service.reject_request(
        db, request_id, reviewer_person_id=current_user["person_id"], review_notes=review_notes
    )
    log_audit_event(
        db=db,
        request=request,
        action="reject",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"review_notes": review_notes},
    )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests/{request_id}", status_code=303
    )


@router.post("/fiber-change-requests/bulk-approve")
def fiber_change_requests_bulk_approve(request: Request, db: Session = Depends(get_db)):
    data = parse_form_data_sync(request)
    request_ids = _form_getlist_str(data, "request_ids")
    force_apply = data.get("force_apply") == "true"
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    skipped = 0
    for request_id in request_ids:
        change_request = change_request_service.get_request(db, request_id)
        if web_network_fiber_service.has_change_request_conflict(db, change_request) and not force_apply:
            skipped += 1
            continue
        change_request_service.approve_request(
            db,
            request_id,
            reviewer_person_id=current_user["person_id"],
            review_notes="Bulk approved",
        )
        log_audit_event(
            db=db,
            request=request,
            action="approve",
            entity_type="fiber_change_request",
            entity_id=str(request_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"force_apply": force_apply, "review_notes": "Bulk approved"},
        )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests?bulk=approved&skipped={skipped}",
        status_code=303,
    )


@router.post("/fiber-map/save-plan")
def fiber_map_save_plan(request: Request, db: Session = Depends(get_db)):
    """Persist a planned route to a project quote."""
    # vendor_service removed during CRM cleanup - this endpoint is disabled
    return JSONResponse({"error": "Route revision feature not available"}, status_code=501)


@router.post("/fiber-map/update-position")
def update_asset_position(request: Request, db: Session = Depends(get_db)):
    """Update position of FDH cabinet or splice closure via drag-and-drop."""
    from fastapi.responses import JSONResponse

    try:
        data: dict[str, object] = parse_json_body_sync(request)
        asset_type = data.get("type")
        asset_id = data.get("id")
        latitude_raw = data.get("latitude")
        longitude_raw = data.get("longitude")

        if not isinstance(asset_type, str) or not isinstance(asset_id, str):
            return JSONResponse({"error": "Missing required fields"}, status_code=400)
        if latitude_raw is None or longitude_raw is None:
            return JSONResponse({"error": "Missing required fields"}, status_code=400)

        latitude = _coerce_float_or_none(latitude_raw)
        longitude = _coerce_float_or_none(longitude_raw)
        if latitude is None or longitude is None:
            return JSONResponse({"error": "Invalid coordinates"}, status_code=400)
        payload, status_code = web_network_fiber_service.update_asset_position(
            db,
            asset_type=asset_type,
            asset_id=asset_id,
            latitude=latitude,
            longitude=longitude,
        )
        return JSONResponse(payload, status_code=status_code)

    except Exception as e:
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/fiber-map/nearest-cabinet")
def find_nearest_cabinet(request: Request, lat: float, lng: float, db: Session = Depends(get_db)):
    """Find nearest FDH cabinet to given coordinates for installation planning."""
    from fastapi.responses import JSONResponse

    payload, status_code = web_network_fiber_service.find_nearest_cabinet_data(
        db,
        lat=lat,
        lng=lng,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-map/plan-options")
def plan_options(request: Request, lat: float, lng: float, db: Session = Depends(get_db)):
    """List nearby cabinets for planning and manual routing."""
    from fastapi.responses import JSONResponse

    payload, status_code = web_network_fiber_service.get_plan_options_data(
        db,
        lat=lat,
        lng=lng,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-map/route")
def plan_route(request: Request, lat: float, lng: float, cabinet_id: str, db: Session = Depends(get_db)):
    """Calculate a fiber route between a point and a cabinet."""
    from fastapi.responses import JSONResponse

    payload, status_code = web_network_fiber_service.get_plan_route_data(
        db,
        lat=lat,
        lng=lng,
        cabinet_id=cabinet_id,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get("/fiber-reports", response_class=HTMLResponse)
def fiber_reports(request: Request, db: Session = Depends(get_db), map_limit: int | None = None):
    """Fiber network deployment reports with asset statistics and customer map."""
    page_data = web_network_fiber_service.get_fiber_reports_data(db, map_limit)
    context = _base_context(request, db, active_page="fiber-reports", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/reports.html", context)


@router.get("/fdh-cabinets", response_class=HTMLResponse)
def fdh_cabinets_list(request: Request, db: Session = Depends(get_db)):
    """List FDH cabinets."""
    page_data = web_network_fdh_service.list_page_data(db)
    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinets.html", context)


@router.get("/fdh-cabinets/new", response_class=HTMLResponse)
def fdh_cabinet_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_fdh_service.build_form_context(
        db,
        cabinet=None,
        action_url="/admin/network/fdh-cabinets",
    )

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)


@router.post("/fdh-cabinets", response_class=HTMLResponse)
def fdh_cabinet_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_fdh_service.parse_form_values(form)
    error = web_network_fdh_service.validate_name(str(values["name"]))

    if error:
        form_context = web_network_fdh_service.build_form_context(
            db,
            cabinet=None,
            action_url="/admin/network/fdh-cabinets",
            error=error,
        )
        context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)

    cabinet = web_network_fdh_service.create_cabinet(db, values)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": cabinet.name, "code": cabinet.code},
    )

    return RedirectResponse(f"/admin/network/fdh-cabinets/{cabinet.id}", status_code=303)


@router.get("/fdh-cabinets/{cabinet_id}/edit", response_class=HTMLResponse)
def fdh_cabinet_edit(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    cabinet = web_network_fdh_service.get_cabinet(db, cabinet_id)
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    form_context = web_network_fdh_service.build_form_context(
        db,
        cabinet=cabinet,
        action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
    )
    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)


@router.post("/fdh-cabinets/{cabinet_id}", response_class=HTMLResponse)
def fdh_cabinet_update(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    cabinet = web_network_fdh_service.get_cabinet(db, cabinet_id)
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(cabinet)
    form = parse_form_data_sync(request)
    values = web_network_fdh_service.parse_form_values(form)
    error = web_network_fdh_service.validate_name(str(values["name"]))

    if error:
        form_context = web_network_fdh_service.build_form_context(
            db,
            cabinet=cabinet,
            action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
            error=error,
        )
        context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)

    web_network_fdh_service.commit_cabinet_update(db, cabinet, values)

    after_snapshot = model_to_dict(cabinet)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/fdh-cabinets/{cabinet.id}", status_code=303)


@router.get("/fdh-cabinets/{cabinet_id}", response_class=HTMLResponse)
def fdh_cabinet_detail(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.detail_page_data(db, cabinet_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update(page_data)
    context["activities"] = build_audit_activities(db, "fdh_cabinet", str(cabinet_id), limit=10)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-detail.html", context)


@router.get("/splitters", response_class=HTMLResponse)
def splitters_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.list_splitters_page_data(db)
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/splitters.html", context)


@router.get("/splitters/new", response_class=HTMLResponse)
def splitter_new(request: Request, fdh_id: str | None = None, db: Session = Depends(get_db)):
    form_context = web_network_fdh_service.build_splitter_form_context(
        db,
        splitter=None,
        action_url="/admin/network/splitters",
        selected_fdh_id=fdh_id,
    )
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post("/splitters", response_class=HTMLResponse)
def splitter_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_fdh_service.parse_splitter_form_values(form)
    error = web_network_fdh_service.validate_splitter_form(db, values)

    if error:
        form_context = web_network_fdh_service.build_splitter_form_context(
            db,
            splitter=None,
            action_url="/admin/network/splitters",
            selected_fdh_id=str(values.get("fdh_id") or "") or None,
            error=error,
        )
        context = _base_context(request, db, active_page="splitters", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    splitter = web_network_fdh_service.create_splitter(db, values)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="splitter",
        entity_id=str(splitter.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": splitter.name, "fdh_id": str(splitter.fdh_id) if splitter.fdh_id else None},
    )

    return RedirectResponse(f"/admin/network/splitters/{splitter.id}", status_code=303)


@router.get("/splitters/{splitter_id}/edit", response_class=HTMLResponse)
def splitter_edit(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    splitter = web_network_fdh_service.get_splitter(db, splitter_id)
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    form_context = web_network_fdh_service.build_splitter_form_context(
        db,
        splitter=splitter,
        action_url=f"/admin/network/splitters/{splitter.id}",
        selected_fdh_id=str(splitter.fdh_id) if splitter.fdh_id else None,
    )
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post("/splitters/{splitter_id}", response_class=HTMLResponse)
def splitter_update(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    splitter = web_network_fdh_service.get_splitter(db, splitter_id)
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(splitter)
    form = parse_form_data_sync(request)
    values = web_network_fdh_service.parse_splitter_form_values(form)
    error = web_network_fdh_service.validate_splitter_form(db, values)

    if error:
        form_context = web_network_fdh_service.build_splitter_form_context(
            db,
            splitter=splitter,
            action_url=f"/admin/network/splitters/{splitter.id}",
            selected_fdh_id=str(values.get("fdh_id") or "") or None,
            error=error,
        )
        context = _base_context(request, db, active_page="splitters", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    web_network_fdh_service.commit_splitter_update(db, splitter, values)

    after_snapshot = model_to_dict(splitter)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="splitter",
        entity_id=str(splitter.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/splitters/{splitter.id}", status_code=303)


@router.get("/splitters/{splitter_id}", response_class=HTMLResponse)
def splitter_detail(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.splitter_detail_page_data(db, splitter_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(page_data)
    context["activities"] = build_audit_activities(db, "splitter", str(splitter_id), limit=10)
    return templates.TemplateResponse("admin/network/fiber/splitter-detail.html", context)


@router.get("/fiber-strands", response_class=HTMLResponse)
def fiber_strands_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_strands_service.list_page_data(db)
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/strands.html", context)


@router.get("/fiber-strands/new", response_class=HTMLResponse)
def fiber_strand_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_strands_service.build_form_context(
        strand=None,
        action_url="/admin/network/fiber-strands",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.post("/fiber-strands", response_class=HTMLResponse)
def fiber_strand_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_strands_service.parse_form_values(form)
    _, error = web_network_strands_service.validate_form_values(values)

    if error:
        strand_data = web_network_strands_service.strand_form_data(values)
        form_context = web_network_strands_service.build_form_context(
            strand=strand_data,
            action_url="/admin/network/fiber-strands",
            error=error,
        )
        context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)

    try:
        strand = web_network_strands_service.create_strand(db, values)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="fiber_strand",
            entity_id=str(strand.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "cable_name": strand.cable_name,
                "strand_number": strand.strand_number,
            },
        )
        return RedirectResponse("/admin/network/fiber-strands", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    strand_data = web_network_strands_service.strand_form_data(values)
    form_context = web_network_strands_service.build_form_context(
        strand=strand_data,
        action_url="/admin/network/fiber-strands",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.get("/fiber-strands/{strand_id}/edit", response_class=HTMLResponse)
def fiber_strand_edit(request: Request, strand_id: str, db: Session = Depends(get_db)):
    strand = web_network_strands_service.get_strand(db, strand_id)
    form_context = web_network_strands_service.build_form_context(
        strand=strand,
        action_url=f"/admin/network/fiber-strands/{strand_id}",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.post("/fiber-strands/{strand_id}", response_class=HTMLResponse)
def fiber_strand_update(request: Request, strand_id: str, db: Session = Depends(get_db)):
    strand = web_network_strands_service.get_strand(db, strand_id)

    form = parse_form_data_sync(request)
    values = web_network_strands_service.parse_form_values(form)
    _, error = web_network_strands_service.validate_form_values(values)

    if error:
        strand_data = web_network_strands_service.strand_form_data(values, strand_id=str(strand.id))
        form_context = web_network_strands_service.build_form_context(
            strand=strand_data,
            action_url=f"/admin/network/fiber-strands/{strand_id}",
            error=error,
        )
        context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)

    try:
        before_snapshot = model_to_dict(strand)
        updated_strand = web_network_strands_service.update_strand(db, strand_id, values)
        after_snapshot = model_to_dict(updated_strand)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="fiber_strand",
            entity_id=str(updated_strand.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/fiber-strands", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    strand_data = web_network_strands_service.strand_form_data(values, strand_id=str(strand.id))
    form_context = web_network_strands_service.build_form_context(
        strand=strand_data,
        action_url=f"/admin/network/fiber-strands/{strand_id}",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.get("/splice-closures", response_class=HTMLResponse)
def splice_closures_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_splice_closures_service.list_page_data(db)
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/splice-closures.html", context)


@router.get("/splice-closures/new", response_class=HTMLResponse)
def splice_closure_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_splice_closures_service.build_form_context(
        closure=None,
        action_url="/admin/network/splice-closures",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)


@router.post("/splice-closures", response_class=HTMLResponse)
def splice_closure_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_form_values(form)
    error = web_network_splice_closures_service.validate_name(values)
    if error:
        form_context = web_network_splice_closures_service.build_form_context(
            closure=None,
            action_url="/admin/network/splice-closures",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)

    closure = web_network_splice_closures_service.create_closure(db, values)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="splice_closure",
        entity_id=str(closure.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": closure.name},
    )

    return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)


@router.get("/splice-closures/{closure_id}/edit", response_class=HTMLResponse)
def splice_closure_edit(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )
    form_context = web_network_splice_closures_service.build_form_context(
        closure=closure,
        action_url=f"/admin/network/splice-closures/{closure.id}",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)


@router.post("/splice-closures/{closure_id}", response_class=HTMLResponse)
def splice_closure_update(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(closure)
    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_form_values(form)
    error = web_network_splice_closures_service.validate_name(values)
    if error:
        form_context = web_network_splice_closures_service.build_form_context(
            closure=closure,
            action_url=f"/admin/network/splice-closures/{closure.id}",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)

    web_network_splice_closures_service.commit_closure_update(db, closure, values)

    after_snapshot = model_to_dict(closure)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="splice_closure",
        entity_id=str(closure.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)


@router.get("/splice-closures/{closure_id}", response_class=HTMLResponse)
def splice_closure_detail(request: Request, closure_id: str, db: Session = Depends(get_db)):
    page_data = web_network_splice_closures_service.detail_page_data(db, closure_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(page_data)
    context["activities"] = build_audit_activities(db, "splice_closure", str(closure_id), limit=10)
    return templates.TemplateResponse("admin/network/fiber/splice-closure-detail.html", context)


@router.get("/splice-closures/{closure_id}/trays/new", response_class=HTMLResponse)
def splice_tray_new(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    form_context = web_network_splice_closures_service.build_tray_form_context(
        closure=closure,
        tray=None,
        action_url=f"/admin/network/splice-closures/{closure_id}/trays",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.get("/splice-closures/{closure_id}/trays", response_class=HTMLResponse)
def splice_tray_redirect(closure_id: str):
    return RedirectResponse(f"/admin/network/splice-closures/{closure_id}", status_code=303)


@router.get("/splice-closures/{closure_id}/trays/{tray_id}/edit", response_class=HTMLResponse)
def splice_tray_edit(
    request: Request,
    closure_id: str,
    tray_id: str,
    db: Session = Depends(get_db),
):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    tray = web_network_splice_closures_service.get_tray(db, closure_id, tray_id)
    if not closure or not tray:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Tray not found"},
            status_code=404,
        )

    form_context = web_network_splice_closures_service.build_tray_form_context(
        closure=closure,
        tray=tray,
        action_url=f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.post("/splice-closures/{closure_id}/trays", response_class=HTMLResponse)
def splice_tray_create(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_tray_form_values(form)
    _, error = web_network_splice_closures_service.validate_tray_form_values(values)
    if error:
        tray_data = web_network_splice_closures_service.tray_form_data(values)
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)

    try:
        tray = web_network_splice_closures_service.create_tray(db, str(closure.id), values)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="tray_created",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"tray_number": tray.tray_number, "tray_name": tray.name},
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except Exception as exc:
        tray_data = web_network_splice_closures_service.tray_form_data(values)
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays",
            error=str(exc),
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.post("/splice-closures/{closure_id}/trays/{tray_id}", response_class=HTMLResponse)
def splice_tray_update(
    request: Request,
    closure_id: str,
    tray_id: str,
    db: Session = Depends(get_db),
):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    tray = web_network_splice_closures_service.get_tray(db, closure_id, tray_id)
    if not closure or not tray:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Tray not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_tray_form_values(form)
    _, error = web_network_splice_closures_service.validate_tray_form_values(values)
    if error:
        tray_data = web_network_splice_closures_service.tray_form_data(values, tray_id=str(tray.id))
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)

    try:
        web_network_splice_closures_service.commit_tray_update(db, tray, values)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="tray_updated",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"tray_number": tray.tray_number, "tray_name": tray.name},
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except Exception as exc:
        tray_data = web_network_splice_closures_service.tray_form_data(values, tray_id=str(tray.id))
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
            error=str(exc),
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.get("/splice-closures/{closure_id}/splices/new", response_class=HTMLResponse)
def splice_new(request: Request, closure_id: str, db: Session = Depends(get_db)):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    if not dependencies:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=None,
        action_url=f"/admin/network/splice-closures/{closure_id}/splices",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.post("/splice-closures/{closure_id}/splices", response_class=HTMLResponse)
def splice_create(request: Request, closure_id: str, db: Session = Depends(get_db)):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    if not dependencies:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )
    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_splice_form_values(form)
    _, error = web_network_splice_closures_service.validate_splice_form_values(values)

    if error:
        splice_data = web_network_splice_closures_service.splice_form_data(values)
        form_context = web_network_splice_closures_service.build_splice_form_context(
            closure=closure,
            trays=trays,
            strands=strands,
            splice=splice_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/splices",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

    try:
        splice = cast(
            FiberSplice,
            web_network_splice_closures_service.create_splice(db, str(closure.id), values),
        )
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="splice_created",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "from_strand_id": str(splice.from_strand_id) if splice.from_strand_id else None,
                "to_strand_id": str(splice.to_strand_id) if splice.to_strand_id else None,
            },
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    splice_data = web_network_splice_closures_service.splice_form_data(values)
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=splice_data,
        action_url=f"/admin/network/splice-closures/{closure_id}/splices",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.get("/splice-closures/{closure_id}/splices/{splice_id}/edit", response_class=HTMLResponse)
def splice_edit(
    request: Request,
    closure_id: str,
    splice_id: str,
    db: Session = Depends(get_db),
):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    splice = web_network_splice_closures_service.get_splice(db, closure_id, splice_id)
    if not dependencies or not splice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Fiber splice not found"},
            status_code=404,
        )
    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=cast(FiberSplice, splice),
        action_url=f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.post("/splice-closures/{closure_id}/splices/{splice_id}", response_class=HTMLResponse)
def splice_update(
    request: Request,
    closure_id: str,
    splice_id: str,
    db: Session = Depends(get_db),
):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    splice = web_network_splice_closures_service.get_splice(db, closure_id, splice_id)
    if not dependencies or not splice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Fiber splice not found"},
            status_code=404,
        )
    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_splice_form_values(form)
    _, error = web_network_splice_closures_service.validate_splice_form_values(values)

    if error:
        splice_data = web_network_splice_closures_service.splice_form_data(values, splice_id=str(splice.id))
        form_context = web_network_splice_closures_service.build_splice_form_context(
            closure=closure,
            trays=trays,
            strands=strands,
            splice=splice_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

    try:
        updated_splice = cast(
            FiberSplice,
            web_network_splice_closures_service.update_splice(db, splice_id, values),
        )
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="splice_updated",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "from_strand_id": str(updated_splice.from_strand_id) if updated_splice.from_strand_id else None,
                "to_strand_id": str(updated_splice.to_strand_id) if updated_splice.to_strand_id else None,
            },
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    splice_data = web_network_splice_closures_service.splice_form_data(values, splice_id=str(splice.id))
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=splice_data,
        action_url=f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

# ==================== Comprehensive Network Map ====================

@router.get("/map", response_class=HTMLResponse)
def comprehensive_network_map(request: Request, db: Session = Depends(get_db)):
    """Comprehensive network map showing all infrastructure and customers."""
    from app.services import network_map as network_map_service

    context = _base_context(request, db, active_page="network-map")
    context.update(network_map_service.build_network_map_context(db))
    return templates.TemplateResponse("admin/network/map.html", context)


# ---- Wireless Site Survey Routes ----

@router.get("/site-survey", response_class=HTMLResponse)
def site_survey_list(request: Request, db: Session = Depends(get_db)):
    """List wireless site surveys."""
    from app.services import wireless_survey as ws_service

    surveys = ws_service.wireless_surveys.list(db, limit=100)
    context = _base_context(request, db, active_page="site-survey")
    context.update({
        "surveys": surveys,
    })
    return templates.TemplateResponse("admin/network/site-survey/index.html", context)


@router.get("/site-survey/new", response_class=HTMLResponse)
def site_survey_new(
    request: Request,
    lat: float | None = None,
    lon: float | None = None,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Create new wireless site survey page."""
    from app.services import wireless_survey as ws_service

    context = _base_context(request, db, active_page="site-survey")
    context.update(
        ws_service.wireless_surveys.build_form_context(
            db, None, lat, lon, subscriber_id
        )
    )
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post("/site-survey/new", response_class=HTMLResponse)
def site_survey_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(None),
    frequency_mhz: float = Form(None),
    default_antenna_height_m: float = Form(10.0),
    default_tx_power_dbm: float = Form(20.0),
    project_id: str = Form(None),
    initial_lat: float | None = Form(None),
    initial_lon: float | None = Form(None),
    subscriber_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create new wireless site survey."""
    from app.services import wireless_survey as ws_service
    actor_id = getattr(request.state, "actor_id", None)
    survey = ws_service.wireless_surveys.create_from_form(
        db,
        name,
        description,
        frequency_mhz,
        default_antenna_height_m,
        default_tx_power_dbm,
        project_id,
        subscriber_id,
        actor_id,
    )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": survey.name},
    )
    redirect_url = ws_service.wireless_surveys.build_post_create_redirect(
        survey.id, initial_lat, initial_lon
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/site-survey/{survey_id}", response_class=HTMLResponse)
def site_survey_detail(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Wireless site survey detail with interactive map."""
    from app.services import wireless_survey as ws_service

    context = _base_context(request, db, active_page="site-survey")
    context.update(ws_service.wireless_surveys.build_detail_context(db, survey_id))
    context["activities"] = build_audit_activities(db, "site_survey", str(survey_id), limit=10)
    return templates.TemplateResponse("admin/network/site-survey/detail.html", context)


@router.get("/site-survey/{survey_id}/edit", response_class=HTMLResponse)
def site_survey_edit(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Edit wireless site survey."""
    from app.services import wireless_survey as ws_service

    survey = ws_service.wireless_surveys.get(db, survey_id)
    context = _base_context(request, db, active_page="site-survey")
    context.update(
        ws_service.wireless_surveys.build_form_context(db, survey, None, None, None)
    )
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post("/site-survey/{survey_id}/edit", response_class=HTMLResponse)
def site_survey_update(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    description: str = Form(None),
    frequency_mhz: float = Form(None),
    default_antenna_height_m: float = Form(10.0),
    default_tx_power_dbm: float = Form(20.0),
    project_id: str = Form(None),
    status: str = Form("draft"),
    db: Session = Depends(get_db),
):
    """Update wireless site survey."""
    from uuid import UUID

    from app.models.wireless_survey import SurveyStatus
    from app.schemas.wireless_survey import WirelessSiteSurveyUpdate
    from app.services import wireless_survey as ws_service

    existing_survey = ws_service.wireless_surveys.get(db, survey_id)
    before_snapshot = model_to_dict(existing_survey)
    payload = WirelessSiteSurveyUpdate(
        name=name,
        description=description,
        frequency_mhz=frequency_mhz,
        default_antenna_height_m=default_antenna_height_m,
        default_tx_power_dbm=default_tx_power_dbm,
        project_id=UUID(project_id) if project_id else None,
        status=SurveyStatus(status),
    )
    updated_survey = ws_service.wireless_surveys.update(db, survey_id, payload)
    after_snapshot = model_to_dict(updated_survey)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="site_survey",
        entity_id=str(updated_survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/{survey_id}/delete")
def site_survey_delete(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Delete wireless site survey."""
    from app.services import wireless_survey as ws_service

    survey = ws_service.wireless_surveys.get(db, survey_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": survey.name},
    )
    ws_service.wireless_surveys.delete(db, survey_id)
    return RedirectResponse("/admin/network/site-survey", status_code=303)


@router.get("/site-survey/{survey_id}/elevation", response_class=HTMLResponse)
def site_survey_elevation_lookup(
    request: Request,
    survey_id: str,
    lat: float,
    lon: float,
    db: Session = Depends(get_db),
):
    """Get elevation for a point (HTMX endpoint)."""
    from fastapi.responses import JSONResponse

    from app.services import dem as dem_service

    result = dem_service.get_elevation(lat, lon)
    return JSONResponse(result)


@router.post("/site-survey/{survey_id}/points")
def site_survey_add_point(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    point_type: str = Form("custom"),
    antenna_height_m: float = Form(10.0),
    db: Session = Depends(get_db),
):
    """Add a point to a survey."""
    from app.models.wireless_survey import SurveyPointType
    from app.schemas.wireless_survey import SurveyPointCreate
    from app.services import wireless_survey as ws_service

    payload = SurveyPointCreate(
        name=name,
        latitude=latitude,
        longitude=longitude,
        point_type=SurveyPointType(point_type),
        antenna_height_m=antenna_height_m,
    )
    point = ws_service.survey_points.create(db, survey_id, payload)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="point_added",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/points/{point_id}/delete")
def site_survey_delete_point(request: Request, point_id: str, db: Session = Depends(get_db)):
    """Delete a survey point."""
    from app.services import wireless_survey as ws_service

    point = ws_service.survey_points.get(db, point_id)
    survey_id = point.survey_id
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="point_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    ws_service.survey_points.delete(db, point_id)
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/{survey_id}/analyze-los")
def site_survey_analyze_los(
    request: Request,
    survey_id: str,
    from_point_id: str = Form(...),
    to_point_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Analyze LOS between two points."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.analyze_path(db, survey_id, from_point_id, to_point_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="los_analyzed",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
            "has_clear_los": los_path.has_clear_los,
        },
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.get("/site-survey/{survey_id}/los/{path_id}")
def site_survey_los_detail(request: Request, survey_id: str, path_id: str, db: Session = Depends(get_db)):
    """Get LOS path detail with elevation profile (JSON)."""
    from fastapi.responses import JSONResponse

    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.get(db, path_id)
    return JSONResponse({
        "id": str(los_path.id),
        "from_point_id": str(los_path.from_point_id),
        "to_point_id": str(los_path.to_point_id),
        "distance_m": los_path.distance_m,
        "bearing_deg": los_path.bearing_deg,
        "has_clear_los": los_path.has_clear_los,
        "fresnel_clearance_pct": los_path.fresnel_clearance_pct,
        "max_obstruction_m": los_path.max_obstruction_m,
        "obstruction_distance_m": los_path.obstruction_distance_m,
        "free_space_loss_db": los_path.free_space_loss_db,
        "estimated_rssi_dbm": los_path.estimated_rssi_dbm,
        "elevation_profile": los_path.elevation_profile,
        "sample_count": los_path.sample_count,
    })


@router.post("/site-survey/los/{path_id}/delete")
def site_survey_delete_los(request: Request, path_id: str, db: Session = Depends(get_db)):
    """Delete a LOS path."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.get(db, path_id)
    survey_id = los_path.survey_id
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="los_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
        },
    )
    ws_service.survey_los.delete(db, path_id)
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)
