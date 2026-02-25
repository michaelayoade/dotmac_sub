"""Admin network core devices web routes."""

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

_format_duration = web_network_core_runtime_service.format_duration
_format_bps = web_network_core_runtime_service.format_bps


def _coerce_uuid_or_none(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


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


@router.get("/network-devices", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def network_devices_consolidated(
    request: Request,
    tab: str = "core",
    search: str | None = None,
    db: Session = Depends(get_db),
):
    """Consolidated view of all network devices - core, OLTs, ONTs/CPE."""
    page_data = web_network_core_devices_service.consolidated_page_data(tab, db, search)
    context = _base_context(request, db, active_page="network-devices")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/network-devices/index.html", context)


@router.get("/backups", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def network_backups_overview(
    request: Request,
    status: str | None = None,
    device_type: str | None = None,
    search: str | None = None,
    stale_hours: int = 24,
    sort: str = "last_backup_asc",
    db: Session = Depends(get_db),
):
    """Global backup status page across NAS and OLT devices."""
    page_data = web_network_core_devices_service.backup_overview_page_data(
        db,
        status=status,
        device_type=device_type,
        search=search,
        stale_hours=stale_hours,
        sort=sort,
    )
    context = _base_context(request, db, active_page="network-backups")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/backups/index.html", context)


@router.get("/core-devices", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_devices_list(
    request: Request,
    role: str | None = None,
    device_type: str | None = None,
    status: str | None = None,
    pop_site_id: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    """List core network devices (routers, switches, access points, etc.)."""
    page_data = web_network_core_devices_service.list_page_data(
        db,
        role,
        status,
        device_type=device_type,
        pop_site_id=pop_site_id,
        search=search,
    )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/index.html", context)


@router.get("/core-devices/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_new(
    request: Request,
    pop_site_id: str | None = None,
    db: Session = Depends(get_db),
):
    selected_pop_site_uuid = _coerce_uuid_or_none(pop_site_id)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        pop_site_id=selected_pop_site_uuid,
    )
    form_context = web_network_core_devices_service.build_form_context(
        device=None,
        pop_sites=pop_sites,
        parent_devices=parent_devices,
        selected_pop_site_id=pop_site_id if selected_pop_site_uuid else None,
        current_device_id=None,
        action_url="/admin/network/core-devices",
    )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.post("/core-devices", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    values = web_network_core_devices_service.parse_form_values(form)
    selected_pop_site_id = str(values.get("pop_site_id")) if values.get("pop_site_id") else None
    selected_pop_site_uuid = _coerce_uuid_or_none(selected_pop_site_id)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        pop_site_id=selected_pop_site_uuid,
    )
    normalized, error = web_network_core_devices_service.validate_values(db, values)
    if error:
        snapshot = web_network_core_devices_service.snapshot_for_form(values)
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
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
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
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
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
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


@router.get("/core-devices/parent-options", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_parent_options(
    request: Request,
    pop_site_id: str | None = None,
    current_device_id: str | None = None,
    selected_parent_id: str | None = None,
    parent_device_id: str | None = None,
    db: Session = Depends(get_db),
):
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        current_device_id=_coerce_uuid_or_none(current_device_id),
        pop_site_id=_coerce_uuid_or_none(pop_site_id),
    )
    return templates.TemplateResponse(
        "admin/network/core-devices/_parent_options.html",
        {
            "request": request,
            "parent_devices": parent_devices,
            "selected_parent_id": selected_parent_id or parent_device_id,
        },
    )


@router.get("/core-devices/{device_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_edit(request: Request, device_id: str, db: Session = Depends(get_db)):
    device = web_network_core_devices_service.get_device(db, device_id)
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        current_device_id=device.id,
        pop_site_id=device.pop_site_id,
    )
    form_context = web_network_core_devices_service.build_form_context(
        device=device,
        pop_sites=pop_sites,
        parent_devices=parent_devices,
        selected_pop_site_id=str(device.pop_site_id) if device.pop_site_id else None,
        current_device_id=str(device.id),
        action_url=f"/admin/network/core-devices/{device.id}",
    )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.get("/core-devices/{device_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/core-devices/{device_id}/snmp-oids", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_snmp_oids(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.snmp_oids_page_data(
        db,
        device_id,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/snmp_oids.html", context)


@router.post("/core-devices/{device_id}/snmp-oids", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_snmp_oid_create(
    device_id: str,
    title: str = Form(...),
    oid: str = Form(...),
    check_interval_seconds: int = Form(60),
    rrd_data_source_type: str = Form("gauge"),
    is_enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.create_snmp_oid_for_device(
        db,
        device_id=device_id,
        title=title,
        oid=oid,
        check_interval_seconds=check_interval_seconds,
        rrd_data_source_type=rrd_data_source_type,
        is_enabled=is_enabled,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/snmp-oids?{key}={msg}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/snmp-oids/snmp-walk", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_snmp_oid_walk(
    request: Request,
    device_id: str,
    base_oid: str = Form(".1.3.6.1.2.1"),
    db: Session = Depends(get_db),
):
    lines, error = web_network_core_devices_service.run_snmp_walk_preview(
        db,
        device_id=device_id,
        base_oid=base_oid,
    )
    page_data = web_network_core_devices_service.snmp_oids_page_data(
        db,
        device_id,
        walk_lines=lines,
        error=error,
        message=None if error else f"SNMP walk returned {len(lines)} lines.",
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/snmp_oids.html", context)


@router.post("/core-devices/{device_id}/snmp-oids/{snmp_oid_id}/poll", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_snmp_oid_poll(
    device_id: str,
    snmp_oid_id: str,
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.poll_snmp_oid_for_device(
        db, device_id=device_id, snmp_oid_id=snmp_oid_id
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/snmp-oids?{key}={msg}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/snmp-oids/{snmp_oid_id}/toggle", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_snmp_oid_toggle(
    device_id: str,
    snmp_oid_id: str,
    is_enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.toggle_snmp_oid_for_device(
        db, device_id=device_id, snmp_oid_id=snmp_oid_id, is_enabled=is_enabled
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/snmp-oids?{key}={msg}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/snmp-oids/{snmp_oid_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_snmp_oid_delete(
    device_id: str,
    snmp_oid_id: str,
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.delete_snmp_oid_for_device(
        db, device_id=device_id, snmp_oid_id=snmp_oid_id
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/snmp-oids?{key}={msg}",
        status_code=303,
    )


@router.get("/core-devices/{device_id}/graphs", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_graphs(
    request: Request,
    device_id: str,
    preview_graph_id: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.bandwidth_graphs_page_data(
        db,
        device_id,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
        preview_graph_id=preview_graph_id,
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/graphs.html", context)


@router.post("/core-devices/{device_id}/graphs", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_graph_create(
    device_id: str,
    title: str = Form(...),
    vertical_axis_title: str = Form("Bandwidth"),
    height_px: int = Form(150),
    is_public: bool = Form(False),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.create_bandwidth_graph_for_device(
        db,
        device_id=device_id,
        title=title,
        vertical_axis_title=vertical_axis_title,
        height_px=height_px,
        is_public=is_public,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/graphs/{graph_id}/sources", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_graph_source_add(
    device_id: str,
    graph_id: str,
    source_device_id: str = Form(...),
    snmp_oid_id: str = Form(...),
    factor: float = Form(1.0),
    color_hex: str = Form("#22c55e"),
    draw_type: str = Form("LINE1"),
    stack_enabled: bool = Form(False),
    value_unit: str = Form("Bps"),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.add_bandwidth_graph_source(
        db,
        device_id=device_id,
        graph_id=graph_id,
        source_device_id=source_device_id,
        snmp_oid_id=snmp_oid_id,
        factor=factor,
        color_hex=color_hex,
        draw_type=draw_type,
        stack_enabled=stack_enabled,
        value_unit=value_unit,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/graphs/{graph_id}/preview", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_graph_preview(
    device_id: str,
    graph_id: str,
    db: Session = Depends(get_db),
):
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?preview_graph_id={graph_id}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/graphs/{graph_id}/toggle-public", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_graph_toggle_public(
    device_id: str,
    graph_id: str,
    is_public: bool = Form(False),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.toggle_bandwidth_graph_public(
        db,
        device_id=device_id,
        graph_id=graph_id,
        is_public=is_public,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/graphs/{graph_id}/clone", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_graph_clone(
    device_id: str,
    graph_id: str,
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.clone_bandwidth_graph_for_device(
        db,
        device_id=device_id,
        graph_id=graph_id,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.get("/core-devices/graphs/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_graphs_dashboard(
    request: Request,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.bandwidth_dashboard_page_data(
        db,
        search=search,
    )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/graphs_dashboard.html", context)


@router.get("/core-devices/{device_id}/backups", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_backups(
    request: Request,
    device_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_page_data(
        db,
        device_id,
        date_from=date_from,
        date_to=date_to,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/backups.html", context)


@router.post("/core-devices/{device_id}/backups/settings", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_backup_settings_update(
    device_id: str,
    enabled: bool = Form(False),
    ssh_username: str = Form(""),
    ssh_password: str | None = Form(None),
    ssh_port: int = Form(22),
    backup_type: str = Form("commands"),
    backup_commands: str | None = Form("export"),
    hours_csv: str | None = Form(None),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.update_backup_settings_for_device(
        db,
        device_id=device_id,
        enabled=enabled,
        ssh_username=ssh_username,
        ssh_password=ssh_password,
        ssh_port=ssh_port,
        backup_type=backup_type,
        backup_commands=backup_commands,
        hours_csv=hours_csv,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/backups?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/core-devices/{device_id}/backups/trigger", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def core_device_backup_trigger(
    device_id: str,
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.trigger_backup_for_core_device(
        db,
        device_id=device_id,
        triggered_by="web",
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/backups?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.get("/core-devices/{device_id}/backups/{backup_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_backup_detail(
    request: Request,
    device_id: str,
    backup_id: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_detail_page_data(
        db,
        device_id=device_id,
        backup_id=backup_id,
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Backup not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/backup_detail.html", context)


@router.get("/core-devices/{device_id}/backups/{backup_id}/download", dependencies=[Depends(require_permission("network:read"))])
def core_device_backup_download(
    device_id: str,
    backup_id: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_detail_page_data(
        db,
        device_id=device_id,
        backup_id=backup_id,
    )
    if not page_data:
        return Response(status_code=404)
    backup = page_data["backup"]
    assert backup is not None
    filename = f"core_device_backup_{device_id}_{backup_id}.txt"
    return Response(
        content=str(backup.config_content or ""),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/core-devices/{device_id}/backups/compare", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def core_device_backup_compare(
    request: Request,
    device_id: str,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_compare_page_data(
        db,
        device_id=device_id,
        backup_id_1=backup_id_1,
        backup_id_2=backup_id_2,
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Unable to compare selected backups"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/backup_compare.html", context)


@router.post("/core-devices/{device_id}/ping", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.post("/core-devices/{device_id}/snmp-check", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.post("/core-devices/{device_id}/snmp-debug", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.get("/core-devices/{device_id}/health", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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
    return HTMLResponse(f'<div id="device-health-content" hx-swap-oob="true">{html}</div>')


@router.post("/core-devices/{device_id}/discover-interfaces", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.post("/core-devices/{device_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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
    selected_pop_site_id = str(values.get("pop_site_id")) if values.get("pop_site_id") else None
    selected_pop_site_uuid = _coerce_uuid_or_none(selected_pop_site_id)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        current_device_id=device.id,
        pop_site_id=selected_pop_site_uuid,
    )
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
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=str(device.id),
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
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=str(device.id),
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
