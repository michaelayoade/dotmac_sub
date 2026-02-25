"""Admin VPN management web routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.db import get_db
from app.services import web_vpn_management as web_vpn_management_service
from app.services import web_vpn_peers as web_vpn_peers_service
from app.services import web_vpn_servers as web_vpn_servers_service
from app.services import wireguard as wg_service
from app.tasks.vpn import run_vpn_control_job, run_vpn_health_scan

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vpn", tags=["web-admin-vpn"])


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
        "csrf_token": get_csrf_token(request),
    }


def _get_actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return str(current_user.get("subscriber_id")) if current_user else None


# ============== WireGuard Dashboard ==============


@router.get("/", response_class=HTMLResponse)
def vpn_index(
    request: Request,
    server_id: str | None = None,
    protocol: str = "wireguard",
    control_job_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Unified VPN dashboard for WireGuard and OpenVPN management."""
    data = web_vpn_management_service.build_unified_dashboard_data(db, server_id=server_id)

    if web_vpn_management_service.should_schedule_health_scan(db):
        run_vpn_health_scan.delay()

    return templates.TemplateResponse(
        "admin/network/vpn/index.html",
        {
            **_base_context(request, db, "vpn"),
            "protocol": protocol if protocol in {"wireguard", "openvpn"} else "wireguard",
            "server": data["wireguard"]["server"],
            "servers": data["wireguard"]["servers_with_counts"],
            "needs_setup": data["wireguard"]["needs_setup"],
            "peers": data["wireguard"]["peers_read"],
            "interface_status": data["wireguard"]["interface_status"],
            "openvpn_clients": data["openvpn_clients"],
            "openvpn_config": data["openvpn_config"],
            "vpn_connections": data["connections"],
            "vpn_summary": data["summary"],
            "vpn_alerts": data["alerts"],
            "control_job_id": control_job_id,
            "success_message": request.query_params.get("success"),
        },
    )


# ============== Server Routes ==============


@router.get("/servers/new", response_class=HTMLResponse)
def server_form_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """New WireGuard server form."""
    vpn_defaults = web_vpn_servers_service.get_vpn_defaults(db)
    return templates.TemplateResponse(
        "admin/network/vpn/server_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": None,
            "errors": [],
            "vpn_defaults": vpn_defaults,
        },
    )


@router.post("/servers/new", response_class=HTMLResponse)
def server_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(None),
    listen_port: int = Form(51820),
    public_host: str = Form(None),
    public_port: int = Form(None),
    vpn_address: str = Form("10.10.0.1/24"),
    vpn_address_v6: str = Form(None),
    mtu: int = Form(1420),
    dns_servers: str = Form(None),
    vpn_routes: str = Form(None),
    is_active: bool = Form(True),
    interface_name: str = Form("wg0"),
    auto_deploy: bool = Form(True),
    router_enabled: bool = Form(False),
    router_host: str = Form(None),
    router_api_port: int = Form(8728),
    router_username: str = Form(None),
    router_password: str = Form(None),
    router_interface_name: str = Form("wg-infra"),
    router_api_ssl: bool = Form(False),
):
    """Create new WireGuard server."""
    server, errors = web_vpn_servers_service.create_server(
        db,
        name=name,
        description=description,
        listen_port=listen_port,
        public_host=public_host,
        public_port=public_port,
        vpn_address=vpn_address,
        vpn_address_v6=vpn_address_v6,
        mtu=mtu,
        dns_servers=dns_servers,
        vpn_routes=vpn_routes,
        is_active=is_active,
        interface_name=interface_name,
        auto_deploy=auto_deploy,
        router_enabled=router_enabled,
        router_host=router_host,
        router_api_port=router_api_port,
        router_username=router_username,
        router_password=router_password,
        router_interface_name=router_interface_name,
        router_api_ssl=router_api_ssl,
        actor_id=_get_actor_id(request),
        request=request,
    )

    if not errors:
        return RedirectResponse(url="/admin/network/vpn", status_code=303)

    vpn_defaults = web_vpn_servers_service.get_vpn_defaults(db)
    return templates.TemplateResponse(
        "admin/network/vpn/server_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": None,
            "errors": errors,
            "vpn_defaults": vpn_defaults,
            "form_data": web_vpn_servers_service.build_form_data(
                name=name,
                description=description,
                listen_port=listen_port,
                public_host=public_host,
                public_port=public_port,
                vpn_address=vpn_address,
                vpn_address_v6=vpn_address_v6,
                mtu=mtu,
                dns_servers=dns_servers,
                vpn_routes=vpn_routes,
                is_active=is_active,
                interface_name=interface_name,
                auto_deploy=auto_deploy,
                router_enabled=router_enabled,
                router_host=router_host,
                router_api_port=router_api_port,
                router_username=router_username,
                router_interface_name=router_interface_name,
                router_api_ssl=router_api_ssl,
            ),
        },
    )


@router.get("/servers/{server_id}/edit", response_class=HTMLResponse)
def server_form_edit(
    server_id: UUID, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Edit WireGuard server form."""
    server = wg_service.wg_servers.get(db, server_id)
    vpn_defaults = web_vpn_servers_service.get_vpn_defaults(db)

    return templates.TemplateResponse(
        "admin/network/vpn/server_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "errors": [],
            "vpn_defaults": vpn_defaults,
        },
    )


@router.post("/servers/{server_id}/edit", response_class=HTMLResponse)
def server_update(
    server_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(None),
    listen_port: int = Form(51820),
    public_host: str = Form(None),
    public_port: int = Form(None),
    vpn_address: str = Form("10.10.0.1/24"),
    vpn_address_v6: str = Form(None),
    mtu: int = Form(1420),
    dns_servers: str = Form(None),
    vpn_routes: str = Form(None),
    is_active: bool = Form(True),
    interface_name: str = Form("wg0"),
    auto_deploy: bool = Form(True),
    router_enabled: bool = Form(False),
    router_host: str = Form(None),
    router_api_port: int = Form(8728),
    router_username: str = Form(None),
    router_password: str = Form(None),
    router_interface_name: str = Form("wg-infra"),
    router_api_ssl: bool = Form(False),
):
    """Update WireGuard server."""
    server, errors = web_vpn_servers_service.update_server(
        db,
        server_id,
        name=name,
        description=description,
        listen_port=listen_port,
        public_host=public_host,
        public_port=public_port,
        vpn_address=vpn_address,
        vpn_address_v6=vpn_address_v6,
        mtu=mtu,
        dns_servers=dns_servers,
        vpn_routes=vpn_routes,
        is_active=is_active,
        interface_name=interface_name,
        auto_deploy=auto_deploy,
        router_enabled=router_enabled,
        router_host=router_host,
        router_api_port=router_api_port,
        router_username=router_username,
        router_password=router_password,
        router_interface_name=router_interface_name,
        router_api_ssl=router_api_ssl,
        actor_id=_get_actor_id(request),
        request=request,
    )

    if not errors:
        return RedirectResponse(url="/admin/network/vpn", status_code=303)

    vpn_defaults = web_vpn_servers_service.get_vpn_defaults(db)
    return templates.TemplateResponse(
        "admin/network/vpn/server_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "errors": errors,
            "vpn_defaults": vpn_defaults,
            "form_data": web_vpn_servers_service.build_form_data(
                name=name,
                description=description,
                listen_port=listen_port,
                public_host=public_host,
                public_port=public_port,
                vpn_address=vpn_address,
                vpn_address_v6=vpn_address_v6,
                mtu=mtu,
                dns_servers=dns_servers,
                vpn_routes=vpn_routes,
                is_active=is_active,
                interface_name=interface_name,
                auto_deploy=auto_deploy,
                router_enabled=router_enabled,
                router_host=router_host,
                router_api_port=router_api_port,
                router_username=router_username,
                router_interface_name=router_interface_name,
                router_api_ssl=router_api_ssl,
            ),
        },
    )


@router.post("/servers/{server_id}/regenerate-keys")
def server_regenerate_keys(
    request: Request,
    server_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Regenerate server keypair."""
    web_vpn_servers_service.regenerate_server_keys(
        db, server_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


@router.post("/servers/{server_id}/deploy")
def server_deploy(
    request: Request, server_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Deploy WireGuard server configuration to the system."""
    web_vpn_servers_service.deploy_server(
        db, server_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


@router.post("/servers/{server_id}/undeploy")
def server_undeploy(
    request: Request, server_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Bring down WireGuard interface."""
    web_vpn_servers_service.undeploy_server(
        db, server_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


@router.post("/servers/{server_id}/test-router")
def server_test_router_connection(
    request: Request,
    server_id: UUID,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Test MikroTik router API connection for a server."""
    _success, body, status_code = web_vpn_servers_service.test_router_connection(
        db, server_id
    )
    return JSONResponse(body, status_code=status_code)


@router.post("/servers/{server_id}/delete")
def server_delete(
    request: Request, server_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete WireGuard server."""
    web_vpn_servers_service.delete_server(
        db, server_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


# ============== Peer Routes ==============


@router.get("/servers/{server_id}/peers/new", response_class=HTMLResponse)
def peer_form_new(
    server_id: UUID, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """New WireGuard peer form."""
    server = wg_service.wg_servers.get(db, server_id)

    return templates.TemplateResponse(
        "admin/network/vpn/peer_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": None,
            "errors": [],
        },
    )


@router.post("/servers/{server_id}/peers/new", response_class=HTMLResponse)
def peer_create(
    server_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(None),
    peer_address: str = Form(None),
    peer_address_v6: str = Form(None),
    persistent_keepalive: int = Form(25),
    use_preshared_key: bool = Form(True),
    known_subnets: str = Form(None),
    lan_subnets: str = Form(None),
    notes: str = Form(None),
):
    """Create new WireGuard peer."""
    created, errors = web_vpn_peers_service.handle_create_peer(
        db,
        server_id,
        name=name,
        description=description,
        peer_address=peer_address,
        peer_address_v6=peer_address_v6,
        persistent_keepalive=persistent_keepalive,
        use_preshared_key=use_preshared_key,
        known_subnets=known_subnets,
        lan_subnets=lan_subnets,
        notes=notes,
        actor_id=_get_actor_id(request),
        request=request,
    )

    if not errors and created:
        return RedirectResponse(
            url=f"/admin/network/vpn/peers/{created.id}?show_keys=true",
            status_code=303,
        )

    server = wg_service.wg_servers.get(db, server_id)
    return templates.TemplateResponse(
        "admin/network/vpn/peer_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": None,
            "errors": errors,
            "form_data": web_vpn_peers_service.build_form_data(
                name=name,
                description=description,
                peer_address=peer_address,
                peer_address_v6=peer_address_v6,
                persistent_keepalive=persistent_keepalive,
                known_subnets=known_subnets,
                lan_subnets=lan_subnets,
                notes=notes,
            ),
        },
    )


@router.get("/peers/{peer_id}", response_class=HTMLResponse)
def peer_detail(
    peer_id: UUID,
    request: Request,
    show_keys: bool = False,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """WireGuard peer detail page."""
    detail = web_vpn_peers_service.get_peer_detail_context(
        db, peer_id, show_keys=show_keys
    )

    return templates.TemplateResponse(
        "admin/network/vpn/peer_detail.html",
        {
            **_base_context(request, db, "vpn"),
            **detail,
        },
    )


@router.get("/peers/{peer_id}/edit", response_class=HTMLResponse)
def peer_form_edit(
    peer_id: UUID, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Edit WireGuard peer form."""
    peer = wg_service.wg_peers.get(db, peer_id)
    server = wg_service.wg_servers.get(db, peer.server_id)

    return templates.TemplateResponse(
        "admin/network/vpn/peer_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": peer,
            "errors": [],
        },
    )


@router.post("/peers/{peer_id}/edit", response_class=HTMLResponse)
def peer_update(
    peer_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    description: str = Form(None),
    peer_address: str = Form(None),
    peer_address_v6: str = Form(None),
    persistent_keepalive: int = Form(25),
    status: str = Form("active"),
    known_subnets: str = Form(None),
    lan_subnets: str = Form(None),
    notes: str = Form(None),
):
    """Update WireGuard peer."""
    updated_peer, peer, server, errors = web_vpn_peers_service.handle_update_peer(
        db,
        peer_id,
        name=name,
        description=description,
        peer_address=peer_address,
        peer_address_v6=peer_address_v6,
        persistent_keepalive=persistent_keepalive,
        status=status,
        known_subnets=known_subnets,
        lan_subnets=lan_subnets,
        notes=notes,
        actor_id=_get_actor_id(request),
        request=request,
    )

    if not errors and updated_peer:
        return RedirectResponse(
            url=f"/admin/network/vpn?server_id={server.id}&success=Peer+updated+successfully",
            status_code=303,
        )

    return templates.TemplateResponse(
        "admin/network/vpn/peer_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": peer,
            "errors": errors,
            "form_data": web_vpn_peers_service.build_form_data(
                name=name,
                description=description,
                peer_address=peer_address,
                peer_address_v6=peer_address_v6,
                persistent_keepalive=persistent_keepalive,
                known_subnets=known_subnets,
                lan_subnets=lan_subnets,
                notes=notes,
            ),
        },
    )


@router.post("/peers/{peer_id}/disable")
def peer_disable(
    request: Request, peer_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Disable WireGuard peer."""
    peer = web_vpn_peers_service.disable_peer(
        db, peer_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(
        url=f"/admin/network/vpn?server_id={peer.server_id}",
        status_code=303,
    )


@router.post("/peers/{peer_id}/enable")
def peer_enable(
    request: Request, peer_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Enable WireGuard peer."""
    peer = web_vpn_peers_service.enable_peer(
        db, peer_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(
        url=f"/admin/network/vpn?server_id={peer.server_id}",
        status_code=303,
    )


@router.post("/peers/{peer_id}/delete")
def peer_delete(
    request: Request, peer_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete WireGuard peer."""
    server_id = web_vpn_peers_service.delete_peer(
        db, peer_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(
        url=f"/admin/network/vpn?server_id={server_id}",
        status_code=303,
    )


@router.post("/peers/{peer_id}/regenerate-token")
def peer_regenerate_token(
    request: Request,
    peer_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Regenerate provisioning token for a peer."""
    web_vpn_peers_service.regenerate_peer_token(
        db, peer_id, actor_id=_get_actor_id(request), request=request
    )
    return RedirectResponse(
        url=f"/admin/network/vpn/peers/{peer_id}",
        status_code=303,
    )


@router.post("/controls/{protocol}/{action}")
def vpn_control_action(
    protocol: str,
    action: str,
    request: Request,
    server_id: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Queue async VPN control action (restart/status/config)."""
    job = web_vpn_management_service.queue_control_job(
        db,
        protocol=protocol,
        action=action,
        server_id=server_id,
        actor_id=_get_actor_id(request),
    )
    run_vpn_control_job.delay(job_id=job["job_id"])
    return RedirectResponse(
        url=f"/admin/network/vpn?protocol={protocol}&server_id={server_id or ''}&control_job_id={job['job_id']}",
        status_code=303,
    )


@router.get("/control-jobs/{job_id}/status", response_class=HTMLResponse)
def vpn_control_job_status(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render polling fragment for VPN control job status."""
    job = web_vpn_management_service.get_control_job(db, job_id)
    return templates.TemplateResponse(
        "admin/network/vpn/_control_job_status.html",
        {
            **_base_context(request, db, "vpn"),
            "job": job,
            "job_id": job_id,
        },
    )


@router.get("/clients/new", response_class=HTMLResponse)
def vpn_client_wizard_form(
    request: Request,
    protocol: str = "wireguard",
    server_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render Add VPN Client wizard for WireGuard/OpenVPN."""
    servers = wg_service.wg_servers.list(db, limit=100)
    return templates.TemplateResponse(
        "admin/network/vpn/client_wizard.html",
        {
            **_base_context(request, db, "vpn"),
            "protocol": protocol if protocol in {"wireguard", "openvpn"} else "wireguard",
            "servers": servers,
            "selected_server_id": server_id or "",
            "errors": [],
            "result": None,
            "openvpn_config": web_vpn_management_service.get_openvpn_server_config(db),
        },
    )


@router.post("/clients/new", response_class=HTMLResponse)
def vpn_client_wizard_submit(
    request: Request,
    db: Session = Depends(get_db),
    protocol: str = Form("wireguard"),
    name: str = Form(...),
    server_id: str | None = Form(None),
    peer_address: str | None = Form(None),
    remote_host: str | None = Form(None),
    remote_port: int | None = Form(None),
) -> HTMLResponse:
    """Create VPN client and generate configuration output."""
    servers = wg_service.wg_servers.list(db, limit=100)
    selected_protocol = protocol if protocol in {"wireguard", "openvpn"} else "wireguard"
    errors: list[str] = []
    result = None

    try:
        result = web_vpn_management_service.create_vpn_client(
            db,
            protocol=selected_protocol,
            name=name.strip(),
            server_id=server_id,
            peer_address=(peer_address or "").strip() or None,
            remote_host=(remote_host or "").strip() or None,
            remote_port=remote_port,
            actor_id=_get_actor_id(request),
            request=request,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        errors.append(str(exc))

    return templates.TemplateResponse(
        "admin/network/vpn/client_wizard.html",
        {
            **_base_context(request, db, "vpn"),
            "protocol": selected_protocol,
            "servers": servers,
            "selected_server_id": server_id or "",
            "errors": errors,
            "result": result,
            "openvpn_config": web_vpn_management_service.get_openvpn_server_config(db),
        },
    )
