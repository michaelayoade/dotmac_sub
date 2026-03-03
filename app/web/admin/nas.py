"""Admin NAS device management web routes."""

from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_nas as web_nas_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/nas", tags=["web-admin-nas"])
DEVICE_FORM_KEYS = [
    "name",
    "vendor",
    "model",
    "ip_address",
    "description",
    "pop_site_id",
    "partner_org_ids",
    "authorization_type",
    "accounting_type",
    "physical_address",
    "latitude",
    "longitude",
    "status",
    "supported_connection_types",
    "default_connection_type",
    "ssh_username",
    "ssh_password",
    "ssh_port",
    "ssh_key",
    "api_url",
    "api_username",
    "api_password",
    "api_key",
    "mikrotik_api_enabled",
    "mikrotik_api_port",
    "snmp_community",
    "snmp_version",
    "snmp_port",
    "backup_enabled",
    "backup_method",
    "backup_schedule",
    "radius_secret",
    "nas_identifier",
    "nas_ip",
    "radius_pool_ids",
    "coa_port",
    "firmware_version",
    "serial_number",
    "location",
    "shaper_enabled",
    "shaper_target",
    "shaping_type",
    "wireless_access_list",
    "disabled_customers_address_list",
    "blocking_rules_enabled",
    "notes",
    "is_active",
]


def _device_form_values(params: dict[str, Any]) -> dict[str, Any]:
    return {key: params.get(key) for key in DEVICE_FORM_KEYS}


# ============== NAS Dashboard ==============


@router.get("/", response_class=HTMLResponse)
def nas_index(
    request: Request,
    db: Session = Depends(get_db),
    vendor: str | None = None,
    nas_type: str | None = None,
    status: str | None = None,
    pop_site_id: str | None = None,
    partner_org_id: str | None = None,
    online_status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
):
    """NAS device management dashboard."""
    context = web_nas_service.dashboard_context(
        request,
        db,
        vendor=vendor,
        nas_type=nas_type,
        status=status,
        pop_site_id=pop_site_id,
        partner_org_id=partner_org_id,
        online_status=online_status,
        search=search,
        page=page,
        limit=25,
    )

    return templates.TemplateResponse(
        "admin/network/nas/index.html",
        context,
    )


# ============== NAS Device CRUD ==============


@router.get("/devices/new", response_class=HTMLResponse)
def device_form_new(request: Request, db: Session = Depends(get_db)):
    """New NAS device form."""
    context = web_nas_service.device_form_context(request, db)
    return templates.TemplateResponse("admin/network/nas/device_form.html", context)


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
    partner_org_ids: list[str] = Form(default=[]),
    authorization_type: str = Form(None),
    accounting_type: str = Form(None),
    physical_address: str = Form(None),
    latitude: str = Form(None),
    longitude: str = Form(None),
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
    mikrotik_api_enabled: bool = Form(False),
    mikrotik_api_port: int = Form(8728),
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
    nas_ip: str = Form(None),
    radius_pool_ids: list[str] = Form(default=[]),
    coa_port: int = Form(3799),
    # Other settings
    firmware_version: str = Form(None),
    serial_number: str = Form(None),
    location: str = Form(None),
    shaper_enabled: bool = Form(False),
    shaper_target: str = Form("this_router"),
    shaping_type: str = Form("simple_queue"),
    wireless_access_list: bool = Form(False),
    disabled_customers_address_list: bool = Form(False),
    blocking_rules_enabled: bool = Form(False),
    notes: str = Form(None),
    is_active: bool = Form(True),
):
    """Create a new NAS device."""
    form_values = _device_form_values(locals())
    result = web_nas_service.create_device(request, db, form_values)
    if result.redirect_url:
        return RedirectResponse(result.redirect_url, status_code=303)
    return templates.TemplateResponse(
        "admin/network/nas/device_form.html", result.context
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(
    request: Request,
    device_id: str,
    tab: str = Query("information"),
    api_test_status: str | None = Query(None),
    api_test_message: str | None = Query(None),
    rule_status: str | None = Query(None),
    rule_message: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """NAS device detail page."""
    context = web_nas_service.device_detail_context(
        request,
        db,
        device_id=device_id,
        tab=tab,
        api_test_status=api_test_status,
        api_test_message=api_test_message,
        rule_status=rule_status,
        rule_message=rule_message,
    )

    return templates.TemplateResponse("admin/network/nas/device_detail.html", context)


@router.get("/devices/{device_id}/edit", response_class=HTMLResponse)
def device_form_edit(request: Request, device_id: str, db: Session = Depends(get_db)):
    """Edit NAS device form."""
    context = web_nas_service.device_form_context(request, db, device_id=device_id)
    return templates.TemplateResponse("admin/network/nas/device_form.html", context)


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
    partner_org_ids: list[str] = Form(default=[]),
    authorization_type: str = Form(None),
    accounting_type: str = Form(None),
    physical_address: str = Form(None),
    latitude: str = Form(None),
    longitude: str = Form(None),
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
    mikrotik_api_enabled: bool = Form(False),
    mikrotik_api_port: int = Form(8728),
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
    nas_ip: str = Form(None),
    radius_pool_ids: list[str] = Form(default=[]),
    coa_port: int = Form(3799),
    # Other settings
    firmware_version: str = Form(None),
    serial_number: str = Form(None),
    location: str = Form(None),
    shaper_enabled: bool = Form(False),
    shaper_target: str = Form("this_router"),
    shaping_type: str = Form("simple_queue"),
    wireless_access_list: bool = Form(False),
    disabled_customers_address_list: bool = Form(False),
    blocking_rules_enabled: bool = Form(False),
    notes: str = Form(None),
    is_active: bool = Form(True),
):
    """Update NAS device."""
    form_values = _device_form_values(locals())
    result = web_nas_service.update_device(request, db, device_id, form_values)
    if result.redirect_url:
        return RedirectResponse(result.redirect_url, status_code=303)
    return templates.TemplateResponse(
        "admin/network/nas/device_form.html", result.context
    )


@router.post("/devices/{device_id}/delete")
def device_delete(request: Request, device_id: str, db: Session = Depends(get_db)):
    """Delete NAS device."""
    result = web_nas_service.delete_device(request, db, device_id)
    return RedirectResponse(result.redirect_url, status_code=303)


@router.post("/devices/{device_id}/ping")
def device_ping(device_id: str, db: Session = Depends(get_db)):
    """Update device last_seen_at timestamp."""
    redirect_url = web_nas_service.device_ping(db, device_id)
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/devices/{device_id}/connection-rules/new")
def device_connection_rule_create(
    device_id: str,
    db: Session = Depends(get_db),
    name: str = Form(...),
    connection_type: str | None = Form(None),
    ip_assignment_mode: str | None = Form(None),
    rate_limit_profile: str | None = Form(None),
    match_expression: str | None = Form(None),
    priority: int = Form(100),
    notes: str | None = Form(None),
):
    """Create a connection rule for a NAS device."""
    redirect_url = web_nas_service.create_connection_rule(
        db,
        device_id=device_id,
        name=name,
        connection_type=connection_type,
        ip_assignment_mode=ip_assignment_mode,
        rate_limit_profile=rate_limit_profile,
        match_expression=match_expression,
        priority=priority,
        notes=notes,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/devices/{device_id}/connection-rules/{rule_id}/toggle")
def device_connection_rule_toggle(
    device_id: str,
    rule_id: str,
    db: Session = Depends(get_db),
    is_active: str = Form(...),
):
    """Toggle active state for a device connection rule."""
    redirect_url = web_nas_service.toggle_connection_rule(
        db,
        device_id=device_id,
        rule_id=rule_id,
        is_active_raw=is_active,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/devices/{device_id}/connection-rules/{rule_id}/delete")
def device_connection_rule_delete(
    device_id: str,
    rule_id: str,
    db: Session = Depends(get_db),
):
    """Delete a device connection rule."""
    redirect_url = web_nas_service.delete_connection_rule(
        db,
        device_id=device_id,
        rule_id=rule_id,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/devices/{device_id}/vendor/mikrotik/test-api")
def device_test_mikrotik_api(device_id: str, db: Session = Depends(get_db)):
    """Run MikroTik API connection/status test."""
    redirect_url = web_nas_service.test_mikrotik_api(db, device_id=device_id)
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/devices/{device_id}/live-bandwidth")
def device_live_bandwidth(device_id: str, db: Session = Depends(get_db)):
    """Open live bandwidth usage view for mapped network device."""
    redirect_url = web_nas_service.live_bandwidth_redirect(db, device_id)
    return RedirectResponse(redirect_url, status_code=303)


# ============== Config Backup Routes ==============


@router.get("/devices/{device_id}/backups", response_class=HTMLResponse)
def device_backups(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
):
    """List configuration backups for a device."""
    context = web_nas_service.device_backups_context(
        request,
        db,
        device_id=device_id,
        page=page,
        limit=25,
    )

    return templates.TemplateResponse("admin/network/nas/backups.html", context)


@router.post("/devices/{device_id}/backups/trigger")
def device_backup_trigger(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
    triggered_by: str = Form("web"),
):
    """Trigger a configuration backup from the device."""
    redirect_url = web_nas_service.trigger_backup(
        request,
        db,
        device_id=device_id,
        triggered_by=triggered_by,
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/backups/{backup_id}", response_class=HTMLResponse)
def backup_detail(request: Request, backup_id: str, db: Session = Depends(get_db)):
    """Config backup detail page."""
    context = web_nas_service.backup_detail_context(
        request,
        db,
        backup_id=backup_id,
    )

    return templates.TemplateResponse("admin/network/nas/backup_detail.html", context)


@router.get("/backups/compare", response_class=HTMLResponse)
def backup_compare(
    request: Request,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
):
    """Compare two configuration backups."""
    context = web_nas_service.backup_compare_context(
        request,
        db,
        backup_id_1=backup_id_1,
        backup_id_2=backup_id_2,
    )

    return templates.TemplateResponse("admin/network/nas/backup_compare.html", context)


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
    context = web_nas_service.templates_list_context(
        request,
        db,
        vendor=vendor,
        action=action,
        page=page,
        limit=25,
    )

    return templates.TemplateResponse("admin/network/nas/templates.html", context)


@router.get("/templates/new", response_class=HTMLResponse)
def template_form_new(request: Request, db: Session = Depends(get_db)):
    """New provisioning template form."""
    context = web_nas_service.template_form_context(request, db)
    return templates.TemplateResponse("admin/network/nas/template_form.html", context)


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
    form_values = {
        "name": name,
        "vendor": vendor,
        "action": action,
        "connection_type": connection_type,
        "template_content": template_content,
        "description": description,
        "placeholders": placeholders,
        "is_active": is_active,
    }
    result = web_nas_service.create_template(request, db, form_values)
    if result.redirect_url:
        return RedirectResponse(result.redirect_url, status_code=303)
    return templates.TemplateResponse(
        "admin/network/nas/template_form.html", result.context
    )


@router.get("/templates/{template_id}", response_class=HTMLResponse)
def template_detail(request: Request, template_id: str, db: Session = Depends(get_db)):
    """Provisioning template detail page."""
    context = web_nas_service.template_detail_context(
        request, db, template_id=template_id
    )

    return templates.TemplateResponse("admin/network/nas/template_detail.html", context)


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
def template_form_edit(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    """Edit provisioning template form."""
    context = web_nas_service.template_form_context(
        request, db, template_id=template_id
    )
    return templates.TemplateResponse("admin/network/nas/template_form.html", context)


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
    form_values = {
        "name": name,
        "vendor": vendor,
        "action": action,
        "connection_type": connection_type,
        "template_content": template_content,
        "description": description,
        "placeholders": placeholders,
        "is_active": is_active,
    }
    result = web_nas_service.update_template(
        request,
        db,
        template_id=template_id,
        form_data=form_values,
    )
    if result.redirect_url:
        return RedirectResponse(result.redirect_url, status_code=303)
    return templates.TemplateResponse(
        "admin/network/nas/template_form.html", result.context
    )


@router.post("/templates/{template_id}/delete")
def template_delete(request: Request, template_id: str, db: Session = Depends(get_db)):
    """Delete provisioning template."""
    result = web_nas_service.delete_template(request, db, template_id)
    return RedirectResponse(result.redirect_url, status_code=303)


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
    context = web_nas_service.logs_list_context(
        request,
        db,
        device_id=device_id,
        action=action,
        status=status,
        page=page,
        limit=50,
    )

    return templates.TemplateResponse("admin/network/nas/logs.html", context)


@router.get("/logs/{log_id}", response_class=HTMLResponse)
def log_detail(request: Request, log_id: str, db: Session = Depends(get_db)):
    """Provisioning log detail page."""
    context = web_nas_service.log_detail_context(
        request,
        db,
        log_id=log_id,
    )

    return templates.TemplateResponse("admin/network/nas/log_detail.html", context)
