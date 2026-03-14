"""Admin network OLT/ONT web routes."""

from datetime import datetime
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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
from app.services import network as network_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services import web_network_onts as web_network_onts_service
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


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }




def _ont_form_dependencies(db: Session) -> dict:
    """Build all dropdown data needed by the ONT provisioning form."""
    deps = web_network_onts_service.ont_form_dependencies(db)
    deps["gpon_channels"] = [e.value for e in GponChannel]
    deps["onu_modes"] = [e.value for e in OnuMode]
    return deps


def _form_uuid_or_none(form: FormData, key: str) -> str | None:
    """Extract a UUID string from form data, returning None if empty."""
    value = form.get(key, "")
    raw = value if isinstance(value, str) else ""
    return raw.strip() or None


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


@router.get("/olts", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def olts_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """List all OLT devices."""
    page_data = web_network_core_devices_service.olts_list_page_data(db)
    context = _base_context(request, db, active_page="olts")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/olts/index.html", context)


@router.get("/olts/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def olt_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="olts")
    context.update({
        "olt": None,
        "action_url": "/admin/network/olts",
        "tr069_servers": web_network_onts_service.get_tr069_servers(db),
    })
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post("/olts", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def olt_create(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    values = web_network_olts_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_olts_service.validate_values(db, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": None,
            "action_url": "/admin/network/olts",
            "error": error,
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    olt, error = web_network_olts_service.create_olt_with_audit(db, request, values, actor_id)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": web_network_olts_service.snapshot(values),
            "action_url": "/admin/network/olts",
            "error": error,
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get("/olts/{olt_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def olt_edit(request: Request, olt_id: str, db: Session = Depends(get_db)):
    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": web_network_olts_service.build_form_model(db, olt),
            "action_url": f"/admin/network/olts/{olt.id}",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post("/olts/{olt_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def olt_update(request: Request, olt_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user

    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    values = web_network_olts_service.parse_form_values(parse_form_data_sync(request))
    error = web_network_olts_service.validate_values(db, values, current_olt=olt)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": web_network_olts_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt.id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    olt, error = web_network_olts_service.update_olt_with_audit(db, request, olt_id, olt, values, actor_id)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": web_network_olts_service.snapshot(values),
            "action_url": f"/admin/network/olts/{olt_id}",
            "error": error,
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get("/olts/{olt_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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
    page_data = web_network_core_devices_service.olt_detail_page_data(db, olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    activities = build_audit_activities(db, "olt", str(olt_id))
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
            "activities": activities,
            "ssh_test_status": ssh_test_status,
            "ssh_test_message": ssh_test_message,
            "snmp_test_status": snmp_test_status,
            "snmp_test_message": snmp_test_message,
            "sync_status": sync_status,
            "sync_message": sync_message,
        }
    )
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.post("/olts/{olt_id}/test-ssh", dependencies=[Depends(require_permission("network:write"))])
def olt_test_ssh_connection(request: Request, olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    from app.web.admin import get_current_user

    ok, message, policy_key = web_network_olts_service.test_olt_ssh_connection(db, olt_id)
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="test_ssh_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "policy_key": policy_key,
            "message": message,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?ssh_test_status={status}&ssh_test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post("/olts/{olt_id}/test-snmp", dependencies=[Depends(require_permission("network:write"))])
def olt_test_snmp_connection(request: Request, olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    from app.web.admin import get_current_user

    ok, message = web_network_olts_service.test_olt_snmp_connection(db, olt_id)
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="test_snmp_connection",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?snmp_test_status={status}&snmp_test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post("/olts/{olt_id}/sync-onts", dependencies=[Depends(require_permission("network:write"))])
def olt_sync_onts(request: Request, olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    from app.web.admin import get_current_user

    ok, message, stats = web_network_olts_service.sync_onts_from_olt_snmp(db, olt_id)
    status = "success" if ok else "error"
    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    log_audit_event(
        db=db,
        request=request,
        action="sync_onts",
        entity_type="olt",
        entity_id=str(olt_id),
        actor_id=actor_id,
        metadata={
            "result": "success" if ok else "error",
            "message": message,
            "stats": stats,
        },
        status_code=200 if ok else 500,
        is_success=ok,
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get("/olts/{olt_id}/sync-onts", dependencies=[Depends(require_permission("network:write"))])
def olt_sync_onts_get_fallback(olt_id: str) -> RedirectResponse:
    """GET fallback for auth-refresh redirects targeting the sync POST endpoint."""
    message = quote_plus("Sync ONTs uses POST. Please click Sync ONTs again.")
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status=info&sync_message={message}",
        status_code=303,
    )


@router.get("/olts/{olt_id}/backups", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def olt_backups_list(
    request: Request,
    olt_id: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    test_status: str | None = None,
    test_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    olt = web_network_olts_service.get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    backups = web_network_olts_service.list_olt_backups(
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


@router.get("/olts/backups/{backup_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def olt_backup_detail(request: Request, backup_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    backup = web_network_olts_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Backup not found"},
            status_code=404,
        )
    olt = web_network_olts_service.get_olt_or_none(db, str(backup.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    preview = web_network_olts_service.read_backup_preview(backup)
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": olt,
            "backup": backup,
            "preview": preview,
        }
    )
    return templates.TemplateResponse("admin/network/olts/backup_detail.html", context)


@router.get("/olts/backups/{backup_id}/download", dependencies=[Depends(require_permission("network:read"))])
def olt_backup_download(backup_id: str, db: Session = Depends(get_db)) -> FileResponse:
    backup = web_network_olts_service.get_olt_backup_or_none(db, backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")
    path = web_network_olts_service.backup_file_path(backup)
    filename = path.name
    return FileResponse(path=path, filename=filename, media_type="text/plain")


@router.post("/olts/{olt_id}/backups/test-connection", dependencies=[Depends(require_permission("network:write"))])
def olt_backup_test_connection(olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    ok, message = web_network_olts_service.test_olt_connection(db, olt_id)
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post("/olts/{olt_id}/backups/test-backup", dependencies=[Depends(require_permission("network:write"))])
def olt_backup_test_backup(olt_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    backup, message = web_network_olts_service.run_test_backup(db, olt_id)
    if backup is not None:
        status = "success"
    else:
        status = "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}/backups?test_status={status}&test_message={quote_plus(message)}",
        status_code=303,
    )


@router.get("/olts/backups/compare", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def olt_backup_compare(
    request: Request,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        backup1, backup2, diff = web_network_olts_service.compare_olt_backups(
            db, backup_id_1, backup_id_2
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    olt = web_network_olts_service.get_olt_or_none(db, str(backup1.olt_device_id))
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context.update({"olt": olt, "backup1": backup1, "backup2": backup2, "diff": diff})
    return templates.TemplateResponse("admin/network/olts/backup_compare.html", context)


@router.get("/onts", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def onts_list(
    request: Request,
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
    return templates.TemplateResponse("admin/network/onts/index.html", context)


@router.get("/onts/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ont_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": None,
        "action_url": "/admin/network/onts",
        **_ont_form_dependencies(db),
    })
    return templates.TemplateResponse("admin/network/onts/form.html", context)


def _ont_unit_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_ont_units_serial_number" in message:
        return "Serial number already exists"
    return "ONT could not be saved due to a data conflict"


@router.post("/onts", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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
            **_ont_form_dependencies(db),
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitCreate(
        serial_number=serial_number,
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
        # SmartOLT fields
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
        context.update({
            "ont": payload,
            "action_url": "/admin/network/onts",
            "error": "New ONTs must be inactive until assigned to a customer.",
            **_ont_form_dependencies(db),
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
            **_ont_form_dependencies(db),
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.get("/onts/{ont_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ont_edit(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": ont,
        "action_url": f"/admin/network/onts/{ont.id}",
        **_ont_form_dependencies(db),
    })
    return templates.TemplateResponse("admin/network/onts/form.html", context)


@router.get("/onts/{ont_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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

    allowed_tabs = {"overview", "network", "history", "tr069", "charts"}
    active_tab = tab if tab in allowed_tabs else "overview"

    activities = build_audit_activities(db, "ont", str(ont_id))
    context = _base_context(request, db, active_page="onts")
    context.update({**page_data, "activities": activities, "ont_active_tab": active_tab})
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get("/onts/{ont_id}/assign", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ont_assign_new(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
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


@router.post("/onts/{ont_id}/assign", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def ont_assign_create(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
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
        context.update({
            "ont": ont,
            **deps,
            "action_url": f"/admin/network/onts/{ont.id}/assign",
            "error": msg,
            "form": web_network_ont_assignments_service.form_payload(values),
        })
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.post("/onts/{ont_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def ont_update(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from app.schemas.network import OntUnitUpdate

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
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
        context.update({
            "ont": ont,
            "action_url": f"/admin/network/onts/{ont.id}",
            "error": "Serial number is required",
            **_ont_form_dependencies(db),
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitUpdate(
        serial_number=serial_number,
        vendor=_form_str(form, "vendor").strip() or None,
        model=_form_str(form, "model").strip() or None,
        firmware_version=_form_str(form, "firmware_version").strip() or None,
        notes=_form_str(form, "notes").strip() or None,
        is_active=_form_str(form, "is_active") == "true",
        # SmartOLT fields
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
            **_ont_form_dependencies(db),
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


# ── ONU Mode / Mgmt IP Modals ──────────────────────────────────────


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
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    vlans = web_network_onts_service.get_vlans(db)
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
        pppoe_password=encrypt_credential(pw) if (pw := _form_str(form, "pppoe_password").strip()) else None,
        wan_remote_access=_form_str(form, "wan_remote_access") == "true",
    )

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get(db=db, unit_id=ont_id)
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
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    vlans = web_network_onts_service.get_vlans(db)
    context = {
        "request": request,
        "ont": ont,
        "vlans": vlans,
        "mgmt_ip_modes": [e.value for e in MgmtIpMode],
    }
    return templates.TemplateResponse(
        "admin/network/onts/_mgmt_ip_modal.html", context
    )


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
        mgmt_vlan_id=_form_uuid_or_none(form, "mgmt_vlan_id"),
        mgmt_ip_address=_form_str(form, "mgmt_ip_address").strip() or None,
        mgmt_remote_access=_form_str(form, "mgmt_remote_access") == "true",
        voip_enabled=_form_str(form, "voip_enabled") == "true",
    )

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    before_snapshot = model_to_dict(ont)
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    after = network_service.ont_units.get(db=db, unit_id=ont_id)
    after_snapshot = model_to_dict(after)
    changes = diff_dicts(before_snapshot, after_snapshot)

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update_mgmt_ip",
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"changes": changes} if changes else None,
    )
    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


# ── ONT Remote Actions ─────────────────────────────────────────────


@router.post("/onts/{ont_id}/reboot", dependencies=[Depends(require_permission("network:write"))])
def ont_reboot(request: Request, ont_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    """Send reboot command to ONT via GenieACS."""
    result = web_network_ont_actions_service.execute_reboot(db, ont_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
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
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post("/onts/{ont_id}/refresh", dependencies=[Depends(require_permission("network:write"))])
def ont_refresh(request: Request, ont_id: str, db: Session = Depends(get_db)) -> JSONResponse:
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
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.get("/onts/{ont_id}/config", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ont_config(request: Request, ont_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    """Fetch and display running config from ONT."""
    result = web_network_ont_actions_service.fetch_running_config(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont_id": ont_id,
        "config_result": result,
    })
    return templates.TemplateResponse(
        "admin/network/onts/_config_partial.html", context
    )


@router.post("/onts/{ont_id}/factory-reset", dependencies=[Depends(require_permission("network:write"))])
def ont_factory_reset(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send factory reset command to ONT via GenieACS."""
    result = web_network_ont_actions_service.execute_factory_reset(db, ont_id)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
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
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post("/onts/{ont_id}/apply-profile", dependencies=[Depends(require_permission("network:write"))])
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
            headers={"HX-Trigger": '{"showToast": {"message": "No profile selected", "type": "error"}}'},
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
        metadata={"profile_id": profile_id, "success": result.success, "fields_updated": result.fields_updated},
    )
    status_code = 200 if result.success else 400
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post("/onts/{ont_id}/wifi-ssid", dependencies=[Depends(require_permission("network:write"))])
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
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post("/onts/{ont_id}/wifi-password", dependencies=[Depends(require_permission("network:write"))])
def ont_set_wifi_password(
    request: Request, ont_id: str, db: Session = Depends(get_db), password: str = Form("")
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
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.post("/onts/{ont_id}/lan-port", dependencies=[Depends(require_permission("network:write"))])
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
    result = web_network_ont_actions_service.toggle_lan_port(
        db, ont_id, port, enabled
    )
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
    headers = {
        "HX-Trigger": '{"showToast": {"message": "'
        + result.message.replace('"', '\\"')
        + '", "type": "'
        + ("success" if result.success else "error")
        + '"}}'
    }
    return JSONResponse(
        {"success": result.success, "message": result.message},
        status_code=status_code,
        headers=headers,
    )


@router.get("/onts/{ont_id}/tr069", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ont_tr069_detail(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069 device details for ONT detail page tab."""
    data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_tr069_partial.html", context
    )


@router.get("/onts/{ont_id}/charts", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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
