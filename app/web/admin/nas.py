"""Admin NAS device management web routes."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.db import get_db
from app.services import nas as nas_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import require_method_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(
    prefix="/nas",
    tags=["web-admin-nas"],
    dependencies=[
        Depends(require_method_permission("network:nas:read", "network:nas:write"))
    ],
)


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
):
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
    return nas_service.get_nas_form_options(db)


def _radius_pool_ids_from_tags(tags: list | None) -> list[str]:
    return nas_service.radius_pool_ids_from_tags(tags)


def _validate_ipv4_address(value: str | None, field_label: str) -> str | None:
    """Backward-compatible helper kept for existing tests/routes."""
    return nas_service.validate_ipv4_address(value, field_label)


def _merge_radius_pool_tags(
    existing_tags: list | None, radius_pool_ids: list[str]
) -> list[str] | None:
    """Backward-compatible helper kept for existing tests/routes."""
    return nas_service.merge_radius_pool_tags(existing_tags, radius_pool_ids)


def _prefixed_values_from_tags(tags: list | None, prefix: str) -> list[str]:
    return nas_service.prefixed_values_from_tags(tags, prefix)


def _extract_enhanced_fields(tags: list | None) -> dict[str, str | list[str] | None]:
    return nas_service.extract_enhanced_fields(tags)


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
    refresh: str | None = None,
    page: int = Query(1, ge=1),
):
    """NAS device management dashboard."""
    page_data = nas_service.build_nas_dashboard_data(
        db,
        vendor=vendor,
        nas_type=nas_type,
        status=status,
        pop_site_id=pop_site_id,
        partner_org_id=partner_org_id,
        online_status=online_status,
        search=search,
        refresh=refresh,
        page=page,
        limit=25,
    )

    return templates.TemplateResponse(
        request,
        "admin/network/nas/index.html",
        {
            **_base_context(request, db, "nas"),
            **page_data,
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
            "selected_radius_pool_ids": [],
            "selected_partner_org_ids": [],
            "enhanced_fields": {},
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
    ssh_port: int = Form(120),
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
    payload, errors = nas_service.build_nas_device_payload(
        db,
        form={
            "name": name,
            "vendor": vendor,
            "model": model,
            "ip_address": ip_address,
            "description": description,
            "pop_site_id": pop_site_id,
            "partner_org_ids": partner_org_ids,
            "authorization_type": authorization_type,
            "accounting_type": accounting_type,
            "physical_address": physical_address,
            "latitude": latitude,
            "longitude": longitude,
            "status": status,
            "supported_connection_types": supported_connection_types,
            "default_connection_type": default_connection_type,
            "ssh_username": ssh_username,
            "ssh_password": ssh_password,
            "ssh_port": ssh_port,
            "ssh_key": ssh_key,
            "api_url": api_url,
            "api_username": api_username,
            "api_password": api_password,
            "api_key": api_key,
            "mikrotik_api_enabled": mikrotik_api_enabled,
            "mikrotik_api_port": mikrotik_api_port,
            "snmp_community": snmp_community,
            "snmp_version": snmp_version,
            "snmp_port": snmp_port,
            "backup_enabled": backup_enabled,
            "backup_method": backup_method,
            "backup_schedule": backup_schedule,
            "radius_secret": radius_secret,
            "nas_identifier": nas_identifier,
            "nas_ip": nas_ip,
            "radius_pool_ids": radius_pool_ids,
            "coa_port": coa_port,
            "firmware_version": firmware_version,
            "serial_number": serial_number,
            "location": location,
            "shaper_enabled": shaper_enabled,
            "shaper_target": shaper_target,
            "shaping_type": shaping_type,
            "wireless_access_list": wireless_access_list,
            "disabled_customers_address_list": disabled_customers_address_list,
            "blocking_rules_enabled": blocking_rules_enabled,
            "notes": notes,
            "is_active": is_active,
        },
        existing_tags=None,
        for_update=False,
    )
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
                "selected_radius_pool_ids": radius_pool_ids,
                "selected_partner_org_ids": partner_org_ids,
            },
        )

    try:
        device = nas_service.create_nas_device_with_audit(
            db,
            request=request,
            payload=payload,
        )
        return RedirectResponse(
            f"/admin/network/nas/devices/{device.id}", status_code=303
        )
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
                "selected_radius_pool_ids": radius_pool_ids,
                "selected_partner_org_ids": partner_org_ids,
            },
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
    page_data = nas_service.build_nas_device_detail_data(
        db,
        device_id=device_id,
        tab=tab,
        api_test_status=api_test_status,
        api_test_message=api_test_message,
        rule_status=rule_status,
        rule_message=rule_message,
        build_activities_fn=build_audit_activities,
    )

    return templates.TemplateResponse(
        "admin/network/nas/device_detail.html",
        {
            **_base_context(request, db, "nas"),
            **page_data,
        },
    )


def _get_pop_site_label(device) -> str | None:
    return nas_service.pop_site_label(device)


def _get_pop_site_label_by_id(db: Session, pop_site_id: str | None) -> str | None:
    return nas_service.pop_site_label_by_id(db, pop_site_id)


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
            "selected_radius_pool_ids": _radius_pool_ids_from_tags(device.tags),
            "selected_partner_org_ids": _prefixed_values_from_tags(
                device.tags, "partner_org:"
            ),
            "enhanced_fields": _extract_enhanced_fields(device.tags),
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
    ssh_port: int = Form(120),
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
    device = nas_service.NasDevices.get(db, device_id)
    payload, errors = nas_service.build_nas_device_payload(
        db,
        form={
            "name": name,
            "vendor": vendor,
            "model": model,
            "ip_address": ip_address,
            "description": description,
            "pop_site_id": pop_site_id,
            "partner_org_ids": partner_org_ids,
            "authorization_type": authorization_type,
            "accounting_type": accounting_type,
            "physical_address": physical_address,
            "latitude": latitude,
            "longitude": longitude,
            "status": status,
            "supported_connection_types": supported_connection_types,
            "default_connection_type": default_connection_type,
            "ssh_username": ssh_username,
            "ssh_password": ssh_password,
            "ssh_port": ssh_port,
            "ssh_key": ssh_key,
            "api_url": api_url,
            "api_username": api_username,
            "api_password": api_password,
            "api_key": api_key,
            "mikrotik_api_enabled": mikrotik_api_enabled,
            "mikrotik_api_port": mikrotik_api_port,
            "snmp_community": snmp_community,
            "snmp_version": snmp_version,
            "snmp_port": snmp_port,
            "backup_enabled": backup_enabled,
            "backup_method": backup_method,
            "backup_schedule": backup_schedule,
            "radius_secret": radius_secret,
            "nas_identifier": nas_identifier,
            "nas_ip": nas_ip,
            "radius_pool_ids": radius_pool_ids,
            "coa_port": coa_port,
            "firmware_version": firmware_version,
            "serial_number": serial_number,
            "location": location,
            "shaper_enabled": shaper_enabled,
            "shaper_target": shaper_target,
            "shaping_type": shaping_type,
            "wireless_access_list": wireless_access_list,
            "disabled_customers_address_list": disabled_customers_address_list,
            "blocking_rules_enabled": blocking_rules_enabled,
            "notes": notes,
            "is_active": is_active,
        },
        existing_tags=device.tags,
        for_update=True,
    )

    if errors:
        return templates.TemplateResponse(
            "admin/network/nas/device_form.html",
            {
                **_base_context(request, db, "nas"),
                **_get_form_options(db),
                "device": device,
                "errors": errors,
                "pop_site_label": _get_pop_site_label(device),
                "form_data": parse_form_data_sync(request),
                "selected_radius_pool_ids": radius_pool_ids,
                "selected_partner_org_ids": partner_org_ids,
                "enhanced_fields": _extract_enhanced_fields(device.tags),
            },
        )

    try:
        nas_service.update_nas_device_with_audit(
            db,
            request=request,
            device_id=device_id,
            payload=payload,
        )
        return RedirectResponse(
            f"/admin/network/nas/devices/{device_id}", status_code=303
        )
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
                "form_data": parse_form_data_sync(request),
                "selected_radius_pool_ids": radius_pool_ids,
                "selected_partner_org_ids": partner_org_ids,
                "enhanced_fields": _extract_enhanced_fields(device.tags),
            },
        )


@router.post("/devices/{device_id}/delete")
def device_delete(request: Request, device_id: str, db: Session = Depends(get_db)):
    """Delete NAS device."""
    nas_service.delete_nas_device_with_audit(
        db,
        request=request,
        device_id=device_id,
    )
    return RedirectResponse("/admin/network/nas", status_code=303)


@router.post("/devices/{device_id}/ping")
def device_ping(device_id: str, db: Session = Depends(get_db)):
    """Update device last_seen_at timestamp."""
    nas_service.ping_nas_device_and_touch_last_seen(db, device_id=device_id)
    return RedirectResponse(f"/admin/network/nas/devices/{device_id}", status_code=303)


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
    return RedirectResponse(
        nas_service.create_connection_rule_redirect_url(
            db,
            device_id=device_id,
            name=name,
            connection_type=connection_type or None,
            ip_assignment_mode=ip_assignment_mode,
            rate_limit_profile=rate_limit_profile,
            match_expression=match_expression,
            priority=priority,
            notes=notes,
        ),
        status_code=303,
    )


@router.post("/devices/{device_id}/connection-rules/{rule_id}/toggle")
def device_connection_rule_toggle(
    device_id: str,
    rule_id: str,
    db: Session = Depends(get_db),
    is_active: str = Form(...),
):
    """Toggle active state for a device connection rule."""
    return RedirectResponse(
        nas_service.toggle_connection_rule_redirect_url(
            db,
            device_id=device_id,
            rule_id=rule_id,
            is_active=is_active,
        ),
        status_code=303,
    )


@router.post("/devices/{device_id}/connection-rules/{rule_id}/delete")
def device_connection_rule_delete(
    device_id: str,
    rule_id: str,
    db: Session = Depends(get_db),
):
    """Delete a device connection rule."""
    return RedirectResponse(
        nas_service.delete_connection_rule_redirect_url(
            db,
            device_id=device_id,
            rule_id=rule_id,
        ),
        status_code=303,
    )


@router.post("/devices/{device_id}/vendor/mikrotik/test-api")
def device_test_mikrotik_api(device_id: str, db: Session = Depends(get_db)):
    """Run MikroTik API connection/status test."""
    return RedirectResponse(
        nas_service.mikrotik_api_test_redirect_url(db, device_id=device_id),
        status_code=303,
    )


@router.post(
    "/devices/{device_id}/vendor/mikrotik/bootstrap-script",
    response_class=PlainTextResponse,
)
def device_generate_mikrotik_bootstrap_script(
    device_id: str, db: Session = Depends(get_db)
):
    """Generate RouterOS bootstrap script and rotate app-side API credentials."""
    data = nas_service.generate_mikrotik_bootstrap_script_for_device(
        db,
        device_id=device_id,
        username="dotmacapi",
        api_port=8728,
        rest_port=443,
    )
    header = [
        "# Dotmac generated MikroTik setup script",
        "# Credentials were saved to this NAS device in Dotmac.",
        f"# API URL: {data['api_url']}",
        f"# Username: {data['username']}",
        "",
    ]
    return PlainTextResponse("\n".join(header) + data["script"] + "\n")


@router.get("/devices/{device_id}/live-bandwidth")
def device_live_bandwidth(device_id: str, db: Session = Depends(get_db)):
    """Open live bandwidth usage view for mapped network device."""
    return RedirectResponse(
        nas_service.live_bandwidth_redirect_url(db, device_id=device_id),
        status_code=303,
    )


# ============== Config Backup Routes ==============


@router.get("/devices/{device_id}/backups", response_class=HTMLResponse)
def device_backups(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
):
    """List configuration backups for a device."""
    page_data = nas_service.build_nas_device_backups_page_data(
        db,
        device_id=device_id,
        page=page,
        limit=25,
    )

    return templates.TemplateResponse(
        "admin/network/nas/backups.html",
        {
            **_base_context(request, db, "nas"),
            **page_data,
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
    result = nas_service.trigger_backup_for_device_with_audit(
        db,
        request=request,
        device_id=device_id,
        triggered_by=triggered_by,
    )
    if result["ok"]:
        return RedirectResponse(
            f"/admin/network/nas/devices/{device_id}?message=Backup+triggered+successfully",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/network/nas/devices/{device_id}?error={result['error']}",
        status_code=303,
    )


@router.get("/backups/{backup_id}", response_class=HTMLResponse)
def backup_detail(request: Request, backup_id: str, db: Session = Depends(get_db)):
    """Config backup detail page."""
    page_data = nas_service.build_nas_backup_detail_data(
        db,
        backup_id=backup_id,
        build_activities_fn=build_audit_activities,
    )

    return templates.TemplateResponse(
        "admin/network/nas/backup_detail.html",
        {
            **_base_context(request, db, "nas"),
            **page_data,
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
    page_data = nas_service.build_nas_backup_compare_data(
        db,
        backup_id_1=backup_id_1,
        backup_id_2=backup_id_2,
    )

    return templates.TemplateResponse(
        "admin/network/nas/backup_compare.html",
        {
            **_base_context(request, db, "nas"),
            **page_data,
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
    page_data = nas_service.build_nas_templates_list_data(
        db,
        vendor=vendor,
        action=action,
        page=page,
        limit=25,
    )

    return templates.TemplateResponse(
        "admin/network/nas/templates.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            **page_data,
        },
    )


@router.get("/templates/new", response_class=HTMLResponse)
def template_form_new(request: Request, db: Session = Depends(get_db)):
    """New provisioning template form."""
    page_data = nas_service.build_nas_template_form_data(db)
    return templates.TemplateResponse(
        "admin/network/nas/template_form.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            **page_data,
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
    payload, errors = nas_service.build_provisioning_template_payload(
        form={
            "name": name,
            "vendor": vendor,
            "action": action,
            "connection_type": connection_type,
            "template_content": template_content,
            "description": description,
            "placeholders": placeholders,
            "is_active": is_active,
        },
        for_update=False,
    )
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
        template = nas_service.create_provisioning_template_with_audit(
            db,
            request=request,
            payload=payload,
        )
        return RedirectResponse(
            f"/admin/network/nas/templates/{template.id}", status_code=303
        )
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
    activities = build_audit_activities(db, "nas_template", template_id, limit=10)

    return templates.TemplateResponse(
        "admin/network/nas/template_detail.html",
        {
            **_base_context(request, db, "nas"),
            "template": template,
            "activities": activities,
        },
    )


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
def template_form_edit(
    request: Request, template_id: str, db: Session = Depends(get_db)
):
    """Edit provisioning template form."""
    page_data = nas_service.build_nas_template_form_data(db, template_id=template_id)
    return templates.TemplateResponse(
        "admin/network/nas/template_form.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            **page_data,
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
    template = nas_service.ProvisioningTemplates.get(db, template_id)
    payload, errors = nas_service.build_provisioning_template_payload(
        form={
            "name": name,
            "vendor": vendor,
            "action": action,
            "connection_type": connection_type,
            "template_content": template_content,
            "description": description,
            "placeholders": placeholders,
            "is_active": is_active,
        },
        for_update=True,
    )
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
        nas_service.update_provisioning_template_with_audit(
            db,
            request=request,
            template_id=template_id,
            payload=payload,
        )
        return RedirectResponse(
            f"/admin/network/nas/templates/{template_id}", status_code=303
        )
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
    nas_service.delete_provisioning_template_with_audit(
        db,
        request=request,
        template_id=template_id,
    )
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
    page_data = nas_service.build_nas_logs_list_data(
        db,
        device_id=device_id,
        action=action,
        status=status,
        page=page,
        limit=50,
    )

    return templates.TemplateResponse(
        "admin/network/nas/logs.html",
        {
            **_base_context(request, db, "nas"),
            **_get_form_options(db),
            **page_data,
        },
    )


@router.get("/logs/{log_id}", response_class=HTMLResponse)
def log_detail(request: Request, log_id: str, db: Session = Depends(get_db)):
    """Provisioning log detail page."""
    page_data = nas_service.build_nas_log_detail_data(
        db,
        log_id=log_id,
        build_activities_fn=build_audit_activities,
    )

    return templates.TemplateResponse(
        "admin/network/nas/log_detail.html",
        {
            **_base_context(request, db, "nas"),
            **page_data,
        },
    )


# ── Monitoring Integration ────────────────────────────────────────────


@router.post("/{device_id}/enable-monitoring")
def enable_monitoring_for_nas(device_id: str, db: Session = Depends(get_db)):
    """Create a NetworkDevice from a NAS device and enable ping/SNMP monitoring."""
    return RedirectResponse(
        nas_service.enable_monitoring_redirect_url(db, device_id=device_id),
        status_code=303,
    )


@router.post("/sync-all-monitoring")
def sync_all_nas_monitoring(db: Session = Depends(get_db)):
    """Sync all active NAS devices into the monitoring system."""
    return RedirectResponse(
        nas_service.sync_all_monitoring_redirect_url(db),
        status_code=303,
    )


# ── NAS VLAN Management ──────────────────────────────────────────


@router.get("/devices/{device_id}/vlans", response_class=HTMLResponse)
def device_vlans(
    request: Request, device_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: VLAN interfaces on this NAS device."""
    from app.services import web_nas_vlan

    try:
        data = web_nas_vlan.vlan_list_context(db, device_id)
    except Exception as exc:
        data = {"device_id": device_id, "vlans": [], "error": str(exc)}

    context = _base_context(request, db, "nas")
    context.update(data)
    return templates.TemplateResponse("admin/network/nas/_vlan_tab.html", context)


@router.post("/devices/{device_id}/vlans/create")
def device_vlan_create(
    request: Request,
    device_id: str,
    vlan_id: int = Form(...),
    parent_interface: str = Form("ether3"),
    ip_address: str = Form(...),
    pppoe_service_name: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create a VLAN + IP + PPPoE server on the NAS device."""
    from app.services import web_nas_vlan

    result = web_nas_vlan.handle_vlan_create_with_audit(
        db,
        request=request,
        device_id=device_id,
        vlan_id=vlan_id,
        parent_interface=parent_interface,
        ip_address=ip_address,
        pppoe_service_name=pppoe_service_name or None,
    )

    msg = quote_plus(result["message"])
    status = "notice" if result["success"] else "error"
    return RedirectResponse(
        f"/admin/network/nas/devices/{device_id}?tab=vlans&{status}={msg}",
        status_code=303,
    )


@router.post("/devices/{device_id}/vlans/{vlan_id}/delete")
def device_vlan_delete(
    request: Request,
    device_id: str,
    vlan_id: int,
    parent_interface: str = Form("ether3"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Remove a VLAN interface (and its IP + PPPoE server) from the NAS."""
    from app.services import web_nas_vlan

    result = web_nas_vlan.handle_vlan_delete_with_audit(
        db,
        request=request,
        device_id=device_id,
        vlan_id=vlan_id,
        parent_interface=parent_interface,
    )

    msg = quote_plus(result["message"])
    status = "notice" if result["success"] else "error"
    return RedirectResponse(
        f"/admin/network/nas/devices/{device_id}?tab=vlans&{status}={msg}",
        status_code=303,
    )
