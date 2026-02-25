"""Admin network IP management and VLAN web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_ip as web_network_ip_service
from app.services import web_network_vlans as web_network_vlans_service
from app.services.audit_helpers import (
    build_audit_activities,
    build_audit_activities_for_types,
    log_audit_event,
)
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network") -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }

@router.get("/ip-management", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/ip-management/pools/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_pool_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update(web_network_ip_service.get_ip_pool_new_form_data())
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/pools/import", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_pool_import_form(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update(
        {
            "default_ip_version": "ipv4",
            "csv_data": "",
            "result": None,
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/pool_import.html", context)


@router.post("/ip-management/pools/import", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def ip_pool_import_submit(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    csv_data = str(form.get("csv_data") or "")
    default_ip_version = str(form.get("default_ip_version") or "ipv4")

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    if not csv_data.strip():
        context.update(
            {
                "default_ip_version": default_ip_version,
                "csv_data": csv_data,
                "result": None,
                "error": "CSV data is required.",
            }
        )
        return templates.TemplateResponse("admin/network/ip-management/pool_import.html", context)

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
    return templates.TemplateResponse("admin/network/ip-management/pool_import.html", context)


@router.get("/ip-management/pools/legacy", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_pools_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.get("/ip-management/blocks/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_block_new(
    request: Request,
    pool_id: str | None = None,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update(web_network_ip_service.get_ip_block_new_form_data(db, pool_id=pool_id))
    return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)


@router.get("/ip-management/blocks", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_blocks_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.post("/ip-management/blocks", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.post("/ip-management/pools", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.get("/ip-management/pools/{pool_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/ip-management/pools/{pool_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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
        "pool": web_network_ip_service.pool_form_snapshot_from_model(pool),
        "action_url": f"/admin/network/ip-management/pools/{pool_id}",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.post("/ip-management/pools/{pool_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
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


@router.get("/ip-management/calculator", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_calculator(request: Request, db: Session = Depends(get_db)):
    """IP subnet calculator tool."""
    context = _base_context(request, db, active_page="ip-calculator", active_menu="ip-address")
    return templates.TemplateResponse("admin/network/ip-management/calculator.html", context)


@router.get("/ip-management/assignments", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/ip-management/dual-stack", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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

    context = _base_context(request, db, active_page="ip-dual-stack", active_menu="ip-address")
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/dual_stack.html", context)


@router.get("/ip-management/ipv4", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/ip-management/ipv4-networks", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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

    context = _base_context(request, db, active_page="ipv4-networks", active_menu="ip-address")
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/ipv4_networks.html", context)


@router.get("/ip-management/ipv4-networks/{pool_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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

    context = _base_context(request, db, active_page="ipv4-networks", active_menu="ip-address")
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/ipv4_network_detail.html", context)


@router.get("/ip-management/ipv6", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/ip-management/ipv6-networks", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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

    context = _base_context(request, db, active_page="ipv6-networks", active_menu="ip-address")
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/ipv6_networks.html", context)


@router.get("/ip-management/ipv6-networks/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ipv6_network_new(request: Request, db: Session = Depends(get_db)):
    """Create form for IPv6 network prefix."""
    context = _base_context(request, db, active_page="ipv6-networks", active_menu="ip-address")
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
    return templates.TemplateResponse("admin/network/ip-management/ipv6_network_form.html", context)


@router.post("/ip-management/ipv6-networks/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def ipv6_network_create(request: Request, db: Session = Depends(get_db)):
    """Create IPv6 network prefix from dedicated form."""
    form = parse_form_data_sync(request)
    pool_values = web_network_ip_service.parse_ipv6_network_form(form)
    error = web_network_ip_service.validate_ip_pool_values(pool_values)

    if error:
        context = _base_context(request, db, active_page="ipv6-networks", active_menu="ip-address")
        context.update(
            {
                "form_values": {
                    "title": str(form.get("title") or ""),
                    "network": str(form.get("network") or ""),
                    "prefix_length": str(form.get("prefix_length") or "64"),
                    "comment": str(form.get("comment") or ""),
                    "location": str(form.get("location") or ""),
                    "category": str(form.get("category") or "Dev"),
                    "network_type": str(form.get("network_type") or "EndNet"),
                    "usage_type": str(form.get("usage_type") or "Static"),
                    "router": str(form.get("router") or ""),
                    "gateway": str(form.get("gateway") or ""),
                    "dns_primary": str(form.get("dns_primary") or ""),
                    "dns_secondary": str(form.get("dns_secondary") or ""),
                    "is_active": form.get("is_active") == "true",
                },
                "error": error,
            }
        )
        return templates.TemplateResponse("admin/network/ip-management/ipv6_network_form.html", context)

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
            metadata={"name": pool.name, "cidr": pool.cidr, "ip_version": "ipv6"},
        )
        return RedirectResponse("/admin/network/ip-management/ipv6-networks", status_code=303)

    context = _base_context(request, db, active_page="ipv6-networks", active_menu="ip-address")
    context.update(
        {
            "form_values": {
                "title": str(form.get("title") or ""),
                "network": str(form.get("network") or ""),
                "prefix_length": str(form.get("prefix_length") or "64"),
                "comment": str(form.get("comment") or ""),
                "location": str(form.get("location") or ""),
                "category": str(form.get("category") or "Dev"),
                "network_type": str(form.get("network_type") or "EndNet"),
                "usage_type": str(form.get("usage_type") or "Static"),
                "router": str(form.get("router") or ""),
                "gateway": str(form.get("gateway") or ""),
                "dns_primary": str(form.get("dns_primary") or ""),
                "dns_secondary": str(form.get("dns_secondary") or ""),
                "is_active": form.get("is_active") == "true",
            },
            "error": error or "Please correct the highlighted fields.",
        }
    )
    return templates.TemplateResponse("admin/network/ip-management/ipv6_network_form.html", context)


@router.get("/ip-management/pools", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def ip_pools_list(
    request: Request,
    pool_type: str = "all",
    db: Session = Depends(get_db),
):
    """List all IP pools and blocks."""
    state = web_network_ip_service.build_ip_pools_data(db, pool_type=pool_type)

    context = _base_context(request, db, active_page="ip-pools", active_menu="ip-address")
    context.update(state)
    return templates.TemplateResponse("admin/network/ip-management/pools.html", context)


@router.get("/vlans", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def vlans_list(request: Request, db: Session = Depends(get_db)):
    """List all VLANs."""
    state = web_network_vlans_service.build_vlans_list_data(db)

    context = _base_context(request, db, active_page="vlans")
    context.update(state)
    return templates.TemplateResponse("admin/network/vlans/index.html", context)


@router.get("/vlans/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def vlan_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="vlans")
    context.update(web_network_vlans_service.build_vlan_new_form_data(db))
    return templates.TemplateResponse("admin/network/vlans/form.html", context)


@router.get("/vlans/{vlan_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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


@router.get("/vlans/{vlan_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
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
