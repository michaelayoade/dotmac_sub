"""Admin NAS device management web routes."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.db import get_db
from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    NasDeviceStatus,
    NasVendor,
    ProvisioningAction,
)
from app.models.network_monitoring import PopSite
from app.schemas.catalog import (
    NasDeviceCreate,
    NasDeviceUpdate,
    ProvisioningTemplateCreate,
    ProvisioningTemplateUpdate,
)
from app.services import nas as nas_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/nas", tags=["web-admin-nas"])

DEVICE_AUDIT_EXCLUDE_FIELDS = {
    "ssh_password",
    "api_password",
    "radius_secret",
    "api_key",
    "ssh_key",
    "snmp_community",
}
TEMPLATE_AUDIT_EXCLUDE_FIELDS = {"template_content"}


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network"):
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


def _get_form_options(db: Session) -> dict:
    """Get options for form dropdowns."""
    pop_sites = db.query(PopSite).filter(PopSite.is_active.is_(True)).order_by(PopSite.name).all()

    return {
        "pop_sites": pop_sites,
        "vendors": [{"value": v.value, "label": v.value.title()} for v in NasVendor],
        "statuses": [{"value": s.value, "label": s.value.title()} for s in NasDeviceStatus],
        "connection_types": [{"value": ct.value, "label": ct.value.upper()} for ct in ConnectionType],
        "backup_methods": [{"value": m.value, "label": m.value.upper()} for m in ConfigBackupMethod],
        "provisioning_actions": [
            {"value": a.value, "label": a.value.replace("_", " ").title()}
            for a in ProvisioningAction
        ],
    }



# ============== NAS Dashboard ==============


@router.get("/", response_class=HTMLResponse)
def nas_index(
    request: Request,
    db: Session = Depends(get_db),
    vendor: str | None = None,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
):
    """NAS device management dashboard."""
    limit = 25
    offset = (page - 1) * limit

    vendor_filter = NasVendor(vendor) if vendor else None
    status_filter = NasDeviceStatus(status) if status else None
    search_filter = search if search else None

    # Get devices with pagination
    devices = nas_service.NasDevices.list(
        db=db,
        limit=limit,
        offset=offset,
        order_by="name",
        order_dir="asc",
        vendor=vendor_filter,
        status=status_filter,
        search=search_filter,
    )

    # Get total count for pagination
    total = nas_service.NasDevices.count(
        db=db,
        vendor=vendor_filter,
        status=status_filter,
        search=search_filter,
    )

    # Get stats
    stats = {
        "by_vendor": nas_service.NasDevices.count_by_vendor(db),
        "by_status": nas_service.NasDevices.count_by_status(db),
    }

    # Calculate pagination
    total_pages = (total + limit - 1) // limit

    return templates.TemplateResponse(
        "admin/network/nas/index.html",
        {
            **_base_context(request, db, "nas"),
            "devices": devices,
            "stats": stats,
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
            "filters": {
                "vendor": vendor,
                "status": status,
                "search": search,
            },
            **_get_form_options(db),
        },
    )


# ============== NAS Device CRUD ==============


@router.get("/devices/new", response_class=HTMLResponse)
def device_form_new(request: Request, db: Session = Depends(get_db)):
    """New NAS device form."""
    return templates.TemplateResponse(
        "admin/network/nas/device_form.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            "device": None,
            "errors": [],
        },
    )


@router.post("/devices/new", response_class=HTMLResponse)
def device_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    vendor: str = Form(...),
    model: str = Form(None),
    ip_address: str = Form(...),
    description: str = Form(None),
    pop_site_id: str = Form(None),
    status: str = Form("active"),
    # Connection settings
    supported_connection_types: str = Form(None),  # JSON array
    default_connection_type: str = Form(None),
    # Management credentials
    ssh_username: str = Form(None),
    ssh_password: str = Form(None),
    ssh_port: int = Form(22),
    ssh_key: str = Form(None),
    api_url: str = Form(None),
    api_username: str = Form(None),
    api_password: str = Form(None),
    api_key: str = Form(None),
    # SNMP settings
    snmp_community: str = Form(None),
    snmp_version: str = Form("2c"),
    snmp_port: int = Form(161),
    # Backup settings
    backup_enabled: bool = Form(False),
    backup_method: str = Form(None),
    backup_schedule: str = Form(None),
    # RADIUS settings
    radius_secret: str = Form(None),
    nas_identifier: str = Form(None),
    coa_port: int = Form(3799),
    # Other settings
    firmware_version: str = Form(None),
    serial_number: str = Form(None),
    location: str = Form(None),
    notes: str = Form(None),
    is_active: bool = Form(True),
):
    """Create a new NAS device."""
    errors = []

    # Parse supported connection types
    conn_types = None
    if supported_connection_types:
        try:
            conn_types_raw = json.loads(supported_connection_types)
            conn_types = [ConnectionType(ct) for ct in conn_types_raw]
        except (json.JSONDecodeError, ValueError) as e:
            errors.append(f"Invalid connection types: {e}")

    if errors:
        return templates.TemplateResponse(
            "admin/network/nas/device_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "device": None,
                "errors": errors,
                "form_data": parse_form_data_sync(request),
                "pop_site_label": _get_pop_site_label_by_id(db, pop_site_id),
            },
        )

    try:
        payload = NasDeviceCreate(
            name=name,
            code=nas_identifier or None,  # nas_identifier maps to code
            vendor=NasVendor(vendor),
            model=model or None,
            ip_address=ip_address,
            management_ip=ip_address,  # Use same IP for management
            management_port=ssh_port,  # ssh_port maps to management_port
            description=description or None,
            pop_site_id=UUID(pop_site_id) if pop_site_id else None,
            rack_position=location or None,  # location maps to rack_position
            status=NasDeviceStatus(status),
            supported_connection_types=[ct.value for ct in conn_types] if conn_types else None,
            default_connection_type=ConnectionType(default_connection_type) if default_connection_type else None,
            ssh_username=ssh_username or None,
            ssh_password=ssh_password or None,
            ssh_key=ssh_key or None,
            api_url=api_url or None,
            api_username=api_username or None,
            api_password=api_password or None,
            api_token=api_key or None,  # api_key maps to api_token
            snmp_community=snmp_community or None,
            snmp_version=snmp_version or None,
            snmp_port=snmp_port,
            backup_enabled=backup_enabled,
            backup_method=ConfigBackupMethod(backup_method) if backup_method else None,
            backup_schedule=backup_schedule or None,
            shared_secret=radius_secret or None,  # radius_secret maps to shared_secret
            coa_port=coa_port,
            firmware_version=firmware_version or None,
            serial_number=serial_number or None,
            notes=notes or None,
            is_active=is_active,
        )
        device = nas_service.NasDevices.create(db, payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="nas_device",
            entity_id=str(device.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": device.name, "ip_address": device.ip_address},
        )
        return RedirectResponse(f"/admin/network/nas/devices/{device.id}", status_code=303)
    except Exception as e:
        errors.append(str(e))
        return templates.TemplateResponse(
            "admin/network/nas/device_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "device": None,
                "errors": errors,
                "form_data": parse_form_data_sync(request),
                "pop_site_label": _get_pop_site_label_by_id(db, pop_site_id),
            },
        )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: str, db: Session = Depends(get_db)):
    """NAS device detail page."""
    device = nas_service.NasDevices.get(db, device_id)

    # Get recent backups
    recent_backups = nas_service.NasConfigBackups.list(
        db, nas_device_id=UUID(device_id), limit=10, offset=0
    )

    # Get recent provisioning logs
    recent_logs = nas_service.ProvisioningLogs.list(
        db, nas_device_id=UUID(device_id), limit=10, offset=0
    )

    activities = build_audit_activities(db, "nas_device", device_id, limit=10)

    return templates.TemplateResponse(
        "admin/network/nas/device_detail.html",
        {
            **_base_context(request, db, "nas"),
            "device": device,
            "backups": recent_backups,
            "logs": recent_logs,
            "activities": activities,
        },
    )


def _get_pop_site_label(device) -> str | None:
    """Get POP site label for typeahead pre-population."""
    if device and device.pop_site:
        label = str(device.pop_site.name)
        if device.pop_site.city:
            label = f"{label} ({str(device.pop_site.city)})"
        return label
    return None


def _get_pop_site_label_by_id(db: Session, pop_site_id: str | None) -> str | None:
    """Get POP site label by ID for typeahead pre-population."""
    if not pop_site_id:
        return None
    try:
        pop_site = db.get(PopSite, UUID(pop_site_id))
        if pop_site:
            label = str(pop_site.name)
            if pop_site.city:
                label = f"{label} ({str(pop_site.city)})"
            return label
    except (ValueError, TypeError):
        pass
    return None


@router.get("/devices/{device_id}/edit", response_class=HTMLResponse)
def device_form_edit(request: Request, device_id: str, db: Session = Depends(get_db)):
    """Edit NAS device form."""
    device = nas_service.NasDevices.get(db, device_id)

    return templates.TemplateResponse(
        "admin/network/nas/device_form.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            "device": device,
            "errors": [],
            "pop_site_label": _get_pop_site_label(device),
        },
    )


@router.post("/devices/{device_id}/edit", response_class=HTMLResponse)
def device_update(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
    name: str = Form(...),
    vendor: str = Form(...),
    model: str = Form(None),
    ip_address: str = Form(...),
    description: str = Form(None),
    pop_site_id: str = Form(None),
    status: str = Form("active"),
    # Connection settings
    supported_connection_types: str = Form(None),
    default_connection_type: str = Form(None),
    # Management credentials
    ssh_username: str = Form(None),
    ssh_password: str = Form(None),
    ssh_port: int = Form(22),
    ssh_key: str = Form(None),
    api_url: str = Form(None),
    api_username: str = Form(None),
    api_password: str = Form(None),
    api_key: str = Form(None),
    # SNMP settings
    snmp_community: str = Form(None),
    snmp_version: str = Form("2c"),
    snmp_port: int = Form(161),
    # Backup settings
    backup_enabled: bool = Form(False),
    backup_method: str = Form(None),
    backup_schedule: str = Form(None),
    # RADIUS settings
    radius_secret: str = Form(None),
    nas_identifier: str = Form(None),
    coa_port: int = Form(3799),
    # Other settings
    firmware_version: str = Form(None),
    serial_number: str = Form(None),
    location: str = Form(None),
    notes: str = Form(None),
    is_active: bool = Form(True),
):
    """Update NAS device."""
    errors = []
    device = nas_service.NasDevices.get(db, device_id)
    before_snapshot = model_to_dict(device, exclude=DEVICE_AUDIT_EXCLUDE_FIELDS)

    # Parse supported connection types
    conn_types = None
    if supported_connection_types:
        try:
            conn_types_raw = json.loads(supported_connection_types)
            conn_types = [ConnectionType(ct) for ct in conn_types_raw]
        except (json.JSONDecodeError, ValueError) as e:
            errors.append(f"Invalid connection types: {e}")

    if errors:
        return templates.TemplateResponse(
            "admin/network/nas/device_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "device": device,
                "errors": errors,
                "pop_site_label": _get_pop_site_label(device),
            },
        )

    try:
        payload = NasDeviceUpdate(
            name=name,
            code=nas_identifier or None,  # nas_identifier maps to code
            vendor=NasVendor(vendor),
            model=model or None,
            ip_address=ip_address,
            management_ip=ip_address,  # Use same IP for management
            management_port=ssh_port,  # ssh_port maps to management_port
            description=description or None,
            pop_site_id=UUID(pop_site_id) if pop_site_id else None,
            rack_position=location or None,  # location maps to rack_position
            status=NasDeviceStatus(status),
            supported_connection_types=[ct.value for ct in conn_types] if conn_types else None,
            default_connection_type=ConnectionType(default_connection_type) if default_connection_type else None,
            ssh_username=ssh_username or None,
            ssh_password=ssh_password or None,
            ssh_key=ssh_key or None,
            api_url=api_url or None,
            api_username=api_username or None,
            api_password=api_password or None,
            api_token=api_key or None,  # api_key maps to api_token
            snmp_community=snmp_community or None,
            snmp_version=snmp_version or None,
            snmp_port=snmp_port,
            backup_enabled=backup_enabled,
            backup_method=ConfigBackupMethod(backup_method) if backup_method else None,
            backup_schedule=backup_schedule or None,
            shared_secret=radius_secret or None,  # radius_secret maps to shared_secret
            coa_port=coa_port,
            firmware_version=firmware_version or None,
            serial_number=serial_number or None,
            notes=notes or None,
            is_active=is_active,
        )
        updated_device = nas_service.NasDevices.update(db, device_id, payload)
        after_snapshot = model_to_dict(updated_device, exclude=DEVICE_AUDIT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="nas_device",
            entity_id=str(updated_device.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse(f"/admin/network/nas/devices/{device_id}", status_code=303)
    except Exception as e:
        errors.append(str(e))
        return templates.TemplateResponse(
            "admin/network/nas/device_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "device": device,
                "errors": errors,
                "pop_site_label": _get_pop_site_label(device),
            },
        )


@router.post("/devices/{device_id}/delete")
def device_delete(request: Request, device_id: str, db: Session = Depends(get_db)):
    """Delete NAS device."""
    device = nas_service.NasDevices.get(db, device_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="nas_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": device.name, "ip_address": device.ip_address},
    )
    nas_service.NasDevices.delete(db, device_id)
    return RedirectResponse("/admin/network/nas", status_code=303)


@router.post("/devices/{device_id}/ping")
def device_ping(device_id: str, db: Session = Depends(get_db)):
    """Update device last_seen_at timestamp."""
    nas_service.NasDevices.update_last_seen(db, device_id)
    return RedirectResponse(f"/admin/network/nas/devices/{device_id}", status_code=303)


# ============== Config Backup Routes ==============


@router.get("/devices/{device_id}/backups", response_class=HTMLResponse)
def device_backups(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
):
    """List configuration backups for a device."""
    device = nas_service.NasDevices.get(db, device_id)

    limit = 25
    offset = (page - 1) * limit

    backups = nas_service.NasConfigBackups.list(
        db, nas_device_id=UUID(device_id), limit=limit, offset=offset
    )

    total = nas_service.NasConfigBackups.count(db, nas_device_id=UUID(device_id))
    total_pages = (total + limit - 1) // limit

    return templates.TemplateResponse(
        "admin/network/nas/backups.html",
        {
            **_base_context(request, db, "nas"),
            "device": device,
            "backups": backups,
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        },
    )


@router.post("/devices/{device_id}/backups/trigger")
def device_backup_trigger(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
    triggered_by: str = Form("web"),
):
    """Trigger a configuration backup from the device."""
    try:
        backup = nas_service.DeviceProvisioner.backup_config(
            db, UUID(device_id), triggered_by
        )
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="backup_triggered",
            entity_type="nas_backup",
            entity_id=str(backup.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "nas_device_id": str(backup.nas_device_id),
                "triggered_by": triggered_by,
            },
        )
        return RedirectResponse(
            f"/admin/network/nas/devices/{device_id}?message=Backup+triggered+successfully",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            f"/admin/network/nas/devices/{device_id}?error={str(e)}",
            status_code=303,
        )


@router.get("/backups/{backup_id}", response_class=HTMLResponse)
def backup_detail(request: Request, backup_id: str, db: Session = Depends(get_db)):
    """Config backup detail page."""
    backup = nas_service.NasConfigBackups.get(db, backup_id)
    device = nas_service.NasDevices.get(db, str(backup.nas_device_id))
    activities = build_audit_activities(db, "nas_backup", backup_id, limit=5)

    return templates.TemplateResponse(
        "admin/network/nas/backup_detail.html",
        {
            **_base_context(request, db, "nas"),
            "backup": backup,
            "device": device,
            "activities": activities,
        },
    )


@router.get("/backups/compare", response_class=HTMLResponse)
def backup_compare(
    request: Request,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
):
    """Compare two configuration backups."""
    result = nas_service.NasConfigBackups.compare(db, UUID(backup_id_1), UUID(backup_id_2))

    backup1 = nas_service.NasConfigBackups.get(db, backup_id_1)
    backup2 = nas_service.NasConfigBackups.get(db, backup_id_2)
    device = nas_service.NasDevices.get(db, str(backup1.nas_device_id))

    return templates.TemplateResponse(
        "admin/network/nas/backup_compare.html",
        {
            **_base_context(request, db, "nas"),
            "backup1": backup1,
            "backup2": backup2,
            "device": device,
            "diff": result,
        },
    )


# ============== Provisioning Template Routes ==============


@router.get("/templates", response_class=HTMLResponse)
def templates_list(
    request: Request,
    db: Session = Depends(get_db),
    vendor: str | None = None,
    action: str | None = None,
    page: int = Query(1, ge=1),
):
    """List provisioning templates."""
    limit = 25
    offset = (page - 1) * limit

    vendor_filter = NasVendor(vendor) if vendor else None
    action_filter = ProvisioningAction(action) if action else None

    provisioning_templates = nas_service.ProvisioningTemplates.list(
        db=db,
        limit=limit,
        offset=offset,
        vendor=vendor_filter,
        action=action_filter,
    )

    total = nas_service.ProvisioningTemplates.count(
        db=db, vendor=vendor_filter, action=action_filter
    )
    total_pages = (total + limit - 1) // limit

    return templates.TemplateResponse(
        "admin/network/nas/templates.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            "templates": provisioning_templates,
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
            "filters": {
                "vendor": vendor,
                "action": action,
            },
        },
    )


@router.get("/templates/new", response_class=HTMLResponse)
def template_form_new(request: Request, db: Session = Depends(get_db)):
    """New provisioning template form."""
    return templates.TemplateResponse(
        "admin/network/nas/template_form.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            "template": None,
            "errors": [],
        },
    )


@router.post("/templates/new", response_class=HTMLResponse)
def template_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    vendor: str = Form(...),
    action: str = Form(...),
    connection_type: str = Form(...),
    template_content: str = Form(...),
    description: str = Form(None),
    placeholders: str = Form(None),  # JSON array
    is_active: bool = Form(True),
):
    """Create a new provisioning template."""
    errors = []

    # Parse placeholders
    placeholder_list = None
    if placeholders:
        try:
            placeholder_list = json.loads(placeholders)
        except json.JSONDecodeError:
            errors.append("Invalid placeholders JSON")

    if errors:
        return templates.TemplateResponse(
            "admin/network/nas/template_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "template": None,
                "errors": errors,
                "form_data": parse_form_data_sync(request),
            },
        )

    try:
        payload = ProvisioningTemplateCreate(
            name=name,
            vendor=NasVendor(vendor),
            action=ProvisioningAction(action),
            connection_type=ConnectionType(connection_type),
            template_content=template_content,
            description=description or None,
            placeholders=placeholder_list,
            is_active=is_active,
        )
        template = nas_service.ProvisioningTemplates.create(db, payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="nas_template",
            entity_id=str(template.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": template.name},
        )
        return RedirectResponse(f"/admin/network/nas/templates/{template.id}", status_code=303)
    except Exception as e:
        errors.append(str(e))
        return templates.TemplateResponse(
            "admin/network/nas/template_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "template": None,
                "errors": errors,
                "form_data": parse_form_data_sync(request),
            },
        )


@router.get("/templates/{template_id}", response_class=HTMLResponse)
def template_detail(request: Request, template_id: str, db: Session = Depends(get_db)):
    """Provisioning template detail page."""
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    activities = build_audit_activities(db, "nas_template", template_id, limit=5)

    return templates.TemplateResponse(
        "admin/network/nas/template_detail.html",
        {
            **_base_context(request, db, "nas"),
            "template": template,
            "activities": activities,
        },
    )


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
def template_form_edit(request: Request, template_id: str, db: Session = Depends(get_db)):
    """Edit provisioning template form."""
    template = nas_service.ProvisioningTemplates.get(db, template_id)

    return templates.TemplateResponse(
        "admin/network/nas/template_form.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            "template": template,
            "errors": [],
        },
    )


@router.post("/templates/{template_id}/edit", response_class=HTMLResponse)
def template_update(
    request: Request,
    template_id: str,
    db: Session = Depends(get_db),
    name: str = Form(...),
    vendor: str = Form(...),
    action: str = Form(...),
    connection_type: str = Form(None),
    template_content: str = Form(...),
    description: str = Form(None),
    placeholders: str = Form(None),
    is_active: bool = Form(True),
):
    """Update provisioning template."""
    errors = []
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    before_snapshot = model_to_dict(template, exclude=TEMPLATE_AUDIT_EXCLUDE_FIELDS)

    # Parse placeholders
    placeholder_list = None
    if placeholders:
        try:
            placeholder_list = json.loads(placeholders)
        except json.JSONDecodeError:
            errors.append("Invalid placeholders JSON")

    if errors:
        return templates.TemplateResponse(
            "admin/network/nas/template_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "template": template,
                "errors": errors,
            },
        )

    try:
        payload = ProvisioningTemplateUpdate(
            name=name,
            vendor=NasVendor(vendor),
            action=ProvisioningAction(action),
            connection_type=ConnectionType(connection_type) if connection_type else None,
            template_content=template_content,
            description=description or None,
            placeholders=placeholder_list,
            is_active=is_active,
        )
        updated_template = nas_service.ProvisioningTemplates.update(db, template_id, payload)
        after_snapshot = model_to_dict(updated_template, exclude=TEMPLATE_AUDIT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="nas_template",
            entity_id=str(updated_template.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse(f"/admin/network/nas/templates/{template_id}", status_code=303)
    except Exception as e:
        errors.append(str(e))
        return templates.TemplateResponse(
            "admin/network/nas/template_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "template": template,
                "errors": errors,
            },
        )


@router.post("/templates/{template_id}/delete")
def template_delete(request: Request, template_id: str, db: Session = Depends(get_db)):
    """Delete provisioning template."""
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="nas_template",
        entity_id=str(template.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": template.name},
    )
    nas_service.ProvisioningTemplates.delete(db, template_id)
    return RedirectResponse("/admin/network/nas/templates", status_code=303)


# ============== Provisioning Log Routes ==============


@router.get("/logs", response_class=HTMLResponse)
def logs_list(
    request: Request,
    db: Session = Depends(get_db),
    device_id: str | None = None,
    action: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
):
    """List provisioning logs."""
    limit = 50
    offset = (page - 1) * limit

    device_filter = UUID(device_id) if device_id else None
    action_filter = ProvisioningAction(action) if action else None
    status_filter = status if status else None

    logs = nas_service.ProvisioningLogs.list(
        db=db,
        limit=limit,
        offset=offset,
        nas_device_id=device_filter,
        action=action_filter,
        status=status_filter,
    )

    total = nas_service.ProvisioningLogs.count(
        db=db,
        nas_device_id=device_filter,
        action=action_filter,
        status=status_filter,
    )
    total_pages = (total + limit - 1) // limit

    # Get devices for filter dropdown
    all_devices = nas_service.NasDevices.list(db, limit=500, offset=0)

    return templates.TemplateResponse(
        "admin/network/nas/logs.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            "logs": logs,
            "devices": all_devices,
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
            "filters": {
                "device_id": device_id,
                "action": action,
                "status": status,
            },
        },
    )


@router.get("/logs/{log_id}", response_class=HTMLResponse)
def log_detail(request: Request, log_id: str, db: Session = Depends(get_db)):
    """Provisioning log detail page."""
    log = nas_service.ProvisioningLogs.get(db, log_id)
    device = nas_service.NasDevices.get(db, str(log.nas_device_id)) if log.nas_device_id else None
    activities = build_audit_activities(db, "nas_provision_log", log_id, limit=5)

    return templates.TemplateResponse(
        "admin/network/nas/log_detail.html",
        {
            **_base_context(request, db, "nas"),
            "log": log,
            "device": device,
            "activities": activities,
        },
    )
