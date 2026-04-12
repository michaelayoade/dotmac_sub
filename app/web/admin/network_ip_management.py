"""Admin network IP management and VLAN web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_ip as web_network_ip_service
from app.services import web_network_ip_actions as web_network_ip_actions_service
from app.services import web_network_vlans as web_network_vlans_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


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


@router.get(
    "/ip-management",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_management(
    request: Request,
    db: Session = Depends(get_db),
    page: int = 1,
    search: str | None = None,
    pool_filter: str | None = None,
    notice: str | None = None,
    warning: str | None = None,
):
    """IP address management page - consolidated view with tabs."""
    state = web_network_ip_service.build_ip_management_data(
        db,
        page=page,
        search=search,
        pool_filter=pool_filter,
    )

    context = _base_context(
        request, db, active_page="ip-management", active_menu="network"
    )
    context.update(
        {
            **state,
            "notice": notice,
            "warning": warning,
            "activities": web_network_ip_actions_service.activity_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/index.html", context)


@router.post(
    "/ip-management/reconcile-pools",
    dependencies=[Depends(require_permission("network:write"))],
)
def reconcile_ip_pool_memberships(request: Request, db: Session = Depends(get_db)):
    redirect_url = web_network_ip_actions_service.reconcile_ipv4_pool_memberships_redirect(
        request,
        db,
    )
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get(
    "/ip-management/pools/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_pool_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(web_network_ip_service.get_ip_pool_new_form_data())
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_form.html", context
    )


@router.get(
    "/ip-management/pools/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_pool_import_form(request: Request, db: Session = Depends(get_db)):
    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(
        {
            "default_ip_version": "ipv4",
            "csv_data": "",
            "result": None,
            "error": None,
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_import.html", context
    )


@router.post(
    "/ip-management/pools/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ip_pool_import_submit(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    csv_data = str(form.get("csv_data") or "")
    default_ip_version = str(form.get("default_ip_version") or "ipv4")

    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    if not csv_data.strip():
        context.update(
            {
                "default_ip_version": default_ip_version,
                "csv_data": csv_data,
                "result": None,
                "error": "CSV data is required.",
            }
        )
        return templates.TemplateResponse(
            "admin/network/ip-management/pool_import.html", context
        )

    result = web_network_ip_service.import_ip_pools_csv(
        db,
        csv_text=csv_data,
        default_ip_version=default_ip_version,
    )
    context.update(
        {
            "default_ip_version": default_ip_version,
            "csv_data": csv_data,
            "result": result,
            "error": None,
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_import.html", context
    )


@router.get(
    "/ip-management/pools/legacy",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_pools_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.get(
    "/ip-management/blocks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_block_new(
    request: Request,
    pool_id: str | None = None,
    db: Session = Depends(get_db),
):
    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(
        web_network_ip_service.get_ip_block_new_form_data(db, pool_id=pool_id)
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/block_form.html", context
    )


@router.get(
    "/ip-management/blocks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_blocks_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.post(
    "/ip-management/blocks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ip_block_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_ip_actions_service.create_ip_block_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.success:
        return RedirectResponse(result.redirect_url or "/admin/network/ip-management", status_code=303)

    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(result.form_context or {})
    return templates.TemplateResponse(
        "admin/network/ip-management/block_form.html", context
    )


@router.post(
    "/ip-management/pools",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ip_pool_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_ip_actions_service.create_ip_pool_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.success:
        return RedirectResponse(result.redirect_url or "/admin/network/ip-management", status_code=303)

    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(result.form_context or {})
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_form.html", context
    )


@router.get(
    "/ip-management/pools/{pool_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_pool_detail(request: Request, pool_id: str, db: Session = Depends(get_db)):
    state = web_network_ip_service.build_ip_pool_detail_data(db, pool_id=pool_id)
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )
    state["pool"]
    activities = web_network_ip_actions_service.activity_for_entity(
        db, "ip_pool", str(pool_id)
    )
    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update({**state, "activities": activities})
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_detail.html", context
    )


@router.get(
    "/ip-management/pools/{pool_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_pool_edit(request: Request, pool_id: str, db: Session = Depends(get_db)):
    pool = web_network_ip_service.get_ip_pool_for_edit(db, pool_id=pool_id)
    if pool is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )

    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(
        {
            "pool": web_network_ip_service.pool_form_snapshot_from_model(pool),
            "action_url": f"/admin/network/ip-management/pools/{pool_id}",
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_form.html", context
    )


@router.post(
    "/ip-management/pools/{pool_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ip_pool_update(request: Request, pool_id: str, db: Session = Depends(get_db)):
    result = web_network_ip_actions_service.update_ip_pool_from_form(
        request,
        db,
        pool_id=pool_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found_message:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )
    if result.success:
        return RedirectResponse(
            result.redirect_url or f"/admin/network/ip-management/pools/{pool_id}",
            status_code=303,
        )

    context = _base_context(
        request, db, active_page="ip-management", active_menu="ip-address"
    )
    context.update(result.form_context or {})
    return templates.TemplateResponse(
        "admin/network/ip-management/pool_form.html", context
    )


@router.get(
    "/ip-management/calculator",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_calculator(request: Request, db: Session = Depends(get_db)):
    """IP subnet calculator tool."""
    context = _base_context(
        request, db, active_page="ip-calculator", active_menu="ip-address"
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/calculator.html", context
    )


@router.get(
    "/ip-management/assignments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_assignments_list(request: Request, db: Session = Depends(get_db)):
    """List all IP assignments."""
    state = web_network_ip_service.build_ip_assignments_data(db)

    context = _base_context(
        request, db, active_page="ip-assignments", active_menu="ip-address"
    )
    context.update(
        {
            **state,
            "activities": web_network_ip_actions_service.activity_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/assignments.html", context
    )


@router.get(
    "/ip-management/dual-stack",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_dual_stack_view(
    request: Request,
    view_mode: str = "subscriber",
    subscriber: str | None = None,
    location: str | None = None,
    db: Session = Depends(get_db),
):
    """Unified IPv4 and IPv6 assignment view by subscriber or location."""
    state = web_network_ip_service.build_dual_stack_data(
        db,
        view_mode=view_mode,
        subscriber_query=subscriber,
        location_query=location,
    )

    context = _base_context(
        request, db, active_page="ip-dual-stack", active_menu="ip-address"
    )
    context.update(state)
    return templates.TemplateResponse(
        "admin/network/ip-management/dual_stack.html", context
    )


@router.get(
    "/ip-management/ipv4",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv4_addresses_list(request: Request, db: Session = Depends(get_db)):
    """List all IPv4 addresses."""
    state = web_network_ip_service.build_ip_addresses_data(db, ip_version="ipv4")

    context = _base_context(
        request, db, active_page="ipv4-addresses", active_menu="ip-address"
    )
    context.update(
        {
            **state,
            "activities": web_network_ip_actions_service.activity_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/addresses.html", context
    )


@router.get(
    "/ip-management/ipv4-networks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv4_networks_list(
    request: Request,
    location: str | None = None,
    category: str | None = None,
    network_type: str | None = None,
    sort_by: str = "cidr",
    sort_dir: str = "asc",
    db: Session = Depends(get_db),
):
    """List IPv4 networks with utilization and metadata."""
    state = web_network_ip_service.build_ipv4_networks_data(
        db,
        location=location,
        category=category,
        network_type=network_type,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )

    context = _base_context(
        request, db, active_page="ipv4-networks", active_menu="ip-address"
    )
    context.update(state)
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv4_networks.html", context
    )


@router.get(
    "/ip-management/ipv4-networks/{pool_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv4_network_detail(request: Request, pool_id: str, db: Session = Depends(get_db)):
    """Detailed IPv4 subnet assignment/status view."""
    state = web_network_ip_service.build_ipv4_network_detail_data(
        db,
        pool_id=pool_id,
        limit=256,
    )
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IPv4 network not found"},
            status_code=404,
        )

    context = _base_context(
        request, db, active_page="ipv4-networks", active_menu="ip-address"
    )
    context.update(state)
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv4_network_detail.html", context
    )


@router.get(
    "/ip-management/ipv4-blocks/{block_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv4_block_detail(request: Request, block_id: str, db: Session = Depends(get_db)):
    """Detailed IPv4 block assignment/status view."""
    state = web_network_ip_service.build_ipv4_block_detail_data(
        db,
        block_id=block_id,
        limit=256,
    )
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IPv4 block not found"},
            status_code=404,
        )

    context = _base_context(
        request, db, active_page="ipv4-networks", active_menu="ip-address"
    )
    context.update(state)
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv4_network_detail.html", context
    )


@router.get(
    "/ip-management/ipv4-assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv4_assignment_form(
    request: Request,
    pool_id: str,
    ip: str,
    block_id: str | None = None,
    return_to: str | None = None,
    db: Session = Depends(get_db),
):
    state = web_network_ip_service.build_ipv4_assignment_form_data(
        db,
        pool_id=pool_id,
        ip_address=ip,
        block_id=block_id,
    )
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IPv4 address not found for this range"},
            status_code=404,
        )

    context = _base_context(
        request, db, active_page="ipv4-networks", active_menu="ip-address"
    )
    context.update(
        {
            **state,
            "return_to": return_to
            or request.headers.get("referer")
            or f"/admin/network/ip-management/ipv4-networks/{pool_id}",
            "action_url": "/admin/network/ip-management/ipv4-assign",
            "error": None,
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv4_assignment_form.html", context
    )


@router.post(
    "/ip-management/ipv4-assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ipv4_assignment_submit(request: Request, db: Session = Depends(get_db)):
    result = web_network_ip_actions_service.assign_ipv4_address_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.not_found_message:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )
    if result.success:
        return RedirectResponse(result.redirect_url or "/admin/network/ip-management", status_code=303)

    if result.form_context:
        context = _base_context(
            request, db, active_page="ipv4-networks", active_menu="ip-address"
        )
        context.update(result.form_context)
        return templates.TemplateResponse(
            "admin/network/ip-management/ipv4_assignment_form.html", context
        )
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.get(
    "/ip-management/ipv6",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv6_addresses_list(request: Request, db: Session = Depends(get_db)):
    """List all IPv6 addresses."""
    state = web_network_ip_service.build_ip_addresses_data(db, ip_version="ipv6")

    context = _base_context(
        request, db, active_page="ipv6-addresses", active_menu="ip-address"
    )
    context.update(
        {
            **state,
            "activities": web_network_ip_actions_service.activity_for_types(
                db,
                ["ip_pool", "ip_block"],
                limit=5,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/addresses.html", context
    )


@router.get(
    "/ip-management/ipv6-networks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv6_networks_list(
    request: Request,
    location: str | None = None,
    category: str | None = None,
    sort_by: str = "cidr",
    sort_dir: str = "asc",
    db: Session = Depends(get_db),
):
    """List IPv6 network prefixes with utilization and metadata."""
    state = web_network_ip_service.build_ipv6_networks_data(
        db,
        location=location,
        category=category,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )

    context = _base_context(
        request, db, active_page="ipv6-networks", active_menu="ip-address"
    )
    context.update(state)
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv6_networks.html", context
    )


@router.get(
    "/ip-management/ipv6-networks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ipv6_network_new(request: Request, db: Session = Depends(get_db)):
    """Create form for IPv6 network prefix."""
    context = _base_context(
        request, db, active_page="ipv6-networks", active_menu="ip-address"
    )
    context.update(
        {
            "form_values": {
                "title": "",
                "network": "",
                "prefix_length": "64",
                "comment": "",
                "location": "",
                "category": "Dev",
                "network_type": "EndNet",
                "usage_type": "Static",
                "router": "",
                "gateway": "",
                "dns_primary": "",
                "dns_secondary": "",
                "is_active": True,
            },
            "error": None,
        }
    )
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv6_network_form.html", context
    )


@router.post(
    "/ip-management/ipv6-networks/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def ipv6_network_create(request: Request, db: Session = Depends(get_db)):
    """Create IPv6 network prefix from dedicated form."""
    result = web_network_ip_actions_service.create_ipv6_network_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if result.success:
        return RedirectResponse(
            result.redirect_url or "/admin/network/ip-management/ipv6-networks",
            status_code=303,
        )

    context = _base_context(
        request, db, active_page="ipv6-networks", active_menu="ip-address"
    )
    context.update(result.form_context or {})
    return templates.TemplateResponse(
        "admin/network/ip-management/ipv6_network_form.html", context
    )


@router.get(
    "/ip-management/pools",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ip_pools_list(
    request: Request,
    pool_type: str = "all",
    db: Session = Depends(get_db),
):
    """List all IP pools and blocks."""
    state = web_network_ip_service.build_ip_pools_data(db, pool_type=pool_type)

    context = _base_context(
        request, db, active_page="ip-pools", active_menu="ip-address"
    )
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/pools.html", context)


@router.get(
    "/vlans",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vlans_list(request: Request, db: Session = Depends(get_db)):
    """List all VLANs."""
    state = web_network_vlans_service.build_vlans_list_data(db)

    context = _base_context(request, db, active_page="vlans")
    context.update(state)
    return templates.TemplateResponse("admin/network/vlans/index.html", context)


@router.get(
    "/vlans/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vlan_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="vlans")
    context.update(web_network_vlans_service.build_vlan_new_form_data(db))
    return templates.TemplateResponse("admin/network/vlans/form.html", context)


@router.post(
    "/vlans",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def vlan_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    try:
        vlan = web_network_vlans_service.handle_vlan_create(db, form)
    except Exception as exc:
        context = _base_context(request, db, active_page="vlans")
        context.update(web_network_vlans_service.build_vlan_new_form_data(db))
        context.update({"error": str(exc), "form_values": dict(form)})
        return templates.TemplateResponse(
            "admin/network/vlans/form.html", context, status_code=400
        )
    return RedirectResponse(f"/admin/network/vlans/{vlan.id}", status_code=303)


@router.get(
    "/vlans/{vlan_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vlan_detail(request: Request, vlan_id: str, db: Session = Depends(get_db)):
    state = web_network_vlans_service.build_vlan_detail_data(db, vlan_id=vlan_id)
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "VLAN not found"},
            status_code=404,
        )

    activities = web_network_ip_actions_service.activity_for_entity(
        db, "vlan", str(vlan_id)
    )
    context = _base_context(request, db, active_page="vlans")
    context.update({**state, "activities": activities})
    return templates.TemplateResponse("admin/network/vlans/detail.html", context)


@router.get(
    "/vlans/{vlan_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def vlan_edit(request: Request, vlan_id: str, db: Session = Depends(get_db)):
    state = web_network_vlans_service.build_vlan_edit_form_data(db, vlan_id=vlan_id)
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "VLAN not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="vlans")
    context.update(state)
    return templates.TemplateResponse("admin/network/vlans/form.html", context)


@router.post(
    "/vlans/{vlan_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def vlan_update(request: Request, vlan_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    try:
        vlan = web_network_vlans_service.handle_vlan_update(
            db, vlan_id=vlan_id, form=form
        )
    except Exception as exc:
        state = web_network_vlans_service.build_vlan_edit_form_data(db, vlan_id=vlan_id)
        if state is None:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "VLAN not found"},
                status_code=404,
            )
        context = _base_context(request, db, active_page="vlans")
        context.update(state)
        context.update({"error": str(exc), "form_values": dict(form)})
        return templates.TemplateResponse(
            "admin/network/vlans/form.html", context, status_code=400
        )
    return RedirectResponse(f"/admin/network/vlans/{vlan.id}", status_code=303)


@router.post(
    "/vlans/{vlan_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def vlan_delete(vlan_id: str, db: Session = Depends(get_db)):
    web_network_vlans_service.handle_vlan_delete(db, vlan_id=vlan_id)
    return RedirectResponse("/admin/network/vlans", status_code=303)
