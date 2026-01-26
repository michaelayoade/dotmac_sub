"""Admin VPN management web routes."""

from datetime import datetime, timezone
import ipaddress
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.csrf import get_csrf_token
from app.db import SessionLocal
from app.models.catalog import NasDevice
from app.models.subscriber import Subscriber
from app.models.wireguard import WireGuardPeerStatus
from app.schemas.wireguard import (
    WireGuardPeerCreate,
    WireGuardPeerUpdate,
    WireGuardServerCreate,
    WireGuardServerUpdate,
)
from app.services import audit as audit_service
from app.services.audit_helpers import (
    diff_dicts,
    extract_changes,
    format_changes,
    log_audit_event,
    model_to_dict,
)
from app.services import wireguard as wg_service
from app.services.wireguard_crypto import encrypt_private_key
from app.models.domain_settings import DomainSetting, SettingDomain

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vpn", tags=["web-admin-vpn"])

PEER_AUDIT_EXCLUDE_FIELDS = {
    "private_key",
    "preshared_key",
    "provision_token_hash",
}
SERVER_AUDIT_EXCLUDE_FIELDS = {"private_key", "metadata_"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_vpn_defaults(db: Session) -> dict:
    """Get VPN default settings from domain settings."""
    defaults = {
        "listen_port": 51820,
        "vpn_address": "10.10.0.1/24",
        "vpn_address_v6": "",
        "mtu": 1420,
        "interface_name": "wg0",
    }
    key_map = {
        "wireguard_default_listen_port": "listen_port",
        "wireguard_default_vpn_address": "vpn_address",
        "wireguard_default_vpn_address_v6": "vpn_address_v6",
        "wireguard_default_mtu": "mtu",
        "wireguard_default_interface_name": "interface_name",
    }
    for setting_key, default_key in key_map.items():
        setting = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == SettingDomain.network)
            .filter(DomainSetting.key == setting_key)
            .filter(DomainSetting.is_active.is_(True))
            .first()
        )
        if setting and setting.value_text:
            value = setting.value_text
            if default_key in ("listen_port", "mtu"):
                try:
                    defaults[default_key] = int(value)
                except ValueError:
                    pass
            else:
                defaults[default_key] = value
    return defaults


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network"):
    from app.web.admin import get_sidebar_stats, get_current_user

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


def _parse_route_cidrs(route_text: str | None) -> tuple[list[str], list[str]]:
    if not route_text:
        return [], []

    raw_items = [item.strip() for item in route_text.replace("\n", ",").split(",")]
    routes = [item for item in raw_items if item]
    errors = []
    normalized = []
    for cidr in routes:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            normalized.append(str(network))
        except ValueError:
            errors.append(f"Invalid route CIDR: {cidr}")
    return normalized, errors


def _parse_known_subnets(route_text: str | None) -> tuple[list[str], list[str]]:
    if not route_text:
        return [], []

    raw_items = [item.strip() for item in route_text.replace("\n", ",").split(",")]
    routes = [item for item in raw_items if item]
    errors = []
    normalized = []
    for cidr in routes:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            normalized.append(str(network))
        except ValueError:
            errors.append(f"Invalid known subnet CIDR: {cidr}")
    return normalized, errors


def _parse_lan_subnets(route_text: str | None) -> tuple[list[str], list[str]]:
    if not route_text:
        return [], []

    raw_items = [item.strip() for item in route_text.replace("\n", ",").split(",")]
    routes = [item for item in raw_items if item]
    errors = []
    normalized = []
    from app.services.vpn_routing import _get_blocked_lan_subnet_networks
    blocked_networks = _get_blocked_lan_subnet_networks()
    for cidr in routes:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            if any(network.overlaps(blocked) for blocked in blocked_networks):
                errors.append(
                    f"LAN subnet {network} overlaps a reserved/local network. "
                    "Choose a different subnet."
                )
                continue
            normalized.append(str(network))
        except ValueError:
            errors.append(f"Invalid LAN subnet CIDR: {cidr}")
    return normalized, errors




def _endpoint_to_ip(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    if endpoint.startswith("[") and "]" in endpoint:
        return endpoint[1:endpoint.index("]")]
    if ":" in endpoint:
        return endpoint.rsplit(":", 1)[0]
    return endpoint


def _build_audit_activities(
    db: Session,
    entity_type: str,
    entity_id: str,
    limit: int = 10,
) -> list[dict]:
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def _sync_peer_stats_from_interface(db: Session, server, interface_status: dict | None) -> None:
    if not interface_status or not interface_status.get("is_up"):
        return
    peers_status = interface_status.get("peers") or []
    if not peers_status:
        return
    status_by_key = {peer["public_key"]: peer for peer in peers_status if peer.get("public_key")}
    if not status_by_key:
        return

    peers = wg_service.wg_peers.list(db, server_id=server.id, limit=1000)
    updated = False
    for peer in peers:
        status = status_by_key.get(peer.public_key)
        if not status:
            continue
        latest_handshake = status.get("latest_handshake")
        if latest_handshake:
            peer.last_handshake_at = datetime.fromtimestamp(latest_handshake, tz=timezone.utc)
        endpoint_ip = _endpoint_to_ip(status.get("endpoint"))
        if endpoint_ip:
            peer.endpoint_ip = endpoint_ip
        peer.rx_bytes = status.get("rx_bytes", peer.rx_bytes or 0)
        peer.tx_bytes = status.get("tx_bytes", peer.tx_bytes or 0)
        updated = True

    if updated:
        db.commit()


# ============== WireGuard Dashboard ==============


@router.get("/", response_class=HTMLResponse)
async def vpn_index(
    request: Request,
    server_id: str | None = None,
    db: Session = Depends(get_db),
):
    """WireGuard management dashboard - shows all servers with peer management."""
    # Get all WireGuard servers
    all_servers = wg_service.wg_servers.list(db, limit=50)

    # If no servers exist, show empty state
    if not all_servers:
        return templates.TemplateResponse(
            "admin/network/vpn/index.html",
            {
                **_base_context(request, db, "vpn"),
                "server": None,
                "servers": [],
                "needs_setup": True,
                "peers": [],
            },
        )

    # Select the active server (from query param or first server)
    if server_id:
        server = wg_service.wg_servers.get(db, server_id)
    else:
        server = all_servers[0]

    # Check if server has keys
    needs_setup = not server.public_key

    # Get interface status
    interface_status = None
    if server.public_key:
        from app.services.wireguard_system import WireGuardSystemService
        interface_status = WireGuardSystemService.get_interface_status(server.interface_name)

    # Sync peer stats from interface and load peers
    _sync_peer_stats_from_interface(db, server, interface_status)
    peers = wg_service.wg_peers.list(db, server_id=server.id, limit=200)
    peers_read = [wg_service.wg_peers.to_read_schema(p, db) for p in peers]

    # Build server info with peer counts
    servers_with_counts = []
    for s in all_servers:
        peer_count = wg_service.wg_servers.get_peer_count(db, s.id)
        servers_with_counts.append({
            "server": s,
            "peer_count": peer_count,
            "is_selected": str(s.id) == str(server.id),
            "needs_setup": not s.public_key,
        })

    return templates.TemplateResponse(
        "admin/network/vpn/index.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "servers": servers_with_counts,
            "needs_setup": needs_setup,
            "peers": peers_read,
            "interface_status": interface_status,
            "success_message": request.query_params.get("success"),
        },
    )


# ============== Server Routes ==============


@router.get("/servers/new", response_class=HTMLResponse)
async def server_form_new(request: Request, db: Session = Depends(get_db)):
    """New WireGuard server form."""
    vpn_defaults = _get_vpn_defaults(db)
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
async def server_create(
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
    errors = []
    vpn_defaults = _get_vpn_defaults(db)

    # Validate router config
    if router_enabled and not router_host:
        errors.append("Router host is required when router sync is enabled")

    # Parse DNS servers
    dns_list = None
    if dns_servers:
        dns_list = [s.strip() for s in dns_servers.split(",") if s.strip()]
    vpn_address = (vpn_address or "").strip()
    if not vpn_address:
        vpn_address = vpn_defaults.get("vpn_address") or "10.10.0.1/24"
    vpn_address_v6 = (vpn_address_v6 or "").strip() or None

    routes_list, route_errors = _parse_route_cidrs(vpn_routes)
    errors.extend(route_errors)

    if errors:
        return templates.TemplateResponse(
            "admin/network/vpn/server_form.html",
            {
                **_base_context(request, db, "vpn"),
                "server": None,
                "errors": errors,
                "vpn_defaults": vpn_defaults,
                "form_data": {
                    "name": name,
                    "description": description,
                    "listen_port": listen_port,
                    "public_host": public_host,
                    "public_port": public_port,
                    "vpn_address": vpn_address,
                    "vpn_address_v6": vpn_address_v6,
                    "mtu": mtu,
                    "dns_servers": dns_servers,
                    "vpn_routes": vpn_routes,
                    "is_active": is_active,
                    "interface_name": interface_name,
                    "auto_deploy": auto_deploy,
                    "router_enabled": router_enabled,
                    "router_host": router_host,
                    "router_api_port": router_api_port,
                    "router_username": router_username,
                    "router_interface_name": router_interface_name,
                    "router_api_ssl": router_api_ssl,
                },
            },
        )

    try:
        payload = WireGuardServerCreate(
            name=name,
            description=description or None,
            listen_port=listen_port,
            public_host=public_host or None,
            public_port=public_port,
            vpn_address=vpn_address,
            vpn_address_v6=vpn_address_v6,
            mtu=mtu,
            dns_servers=dns_list,
            is_active=is_active,
            interface_name=interface_name,
        )
        server = wg_service.wg_servers.create(db, payload)

        # Store auto_deploy and router settings in metadata
        server.metadata_ = server.metadata_ or {}
        server.metadata_["auto_deploy"] = auto_deploy
        if routes_list:
            server.metadata_["routes"] = routes_list
        else:
            server.metadata_.pop("routes", None)

        # Store router configuration if enabled
        if router_enabled and router_host:
            router_config = {
                "enabled": True,
                "host": router_host,
                "api_port": router_api_port,
                "username": router_username or "",
                "interface_name": router_interface_name or "wg-infra",
                "api_ssl": router_api_ssl,
            }
            # Encrypt password before storage
            if router_password:
                router_config["password"] = encrypt_private_key(router_password)
            server.metadata_["router"] = router_config
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(server, "metadata_")

        db.commit()
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="wireguard_server",
            entity_id=str(server.id),
            actor_id=str(current_user.get("person_id")) if current_user else None,
            metadata={"name": server.name},
        )

        # Deploy WireGuard interface if auto_deploy is enabled
        if auto_deploy and server.public_key:
            try:
                from app.services.wireguard_system import WireGuardSystemService
                WireGuardSystemService.deploy_server(db, server.id)
            except Exception as e:
                # Log but don't fail - user can manually deploy later
                import logging
                logging.getLogger(__name__).warning(f"Auto-deploy failed: {e}")

        return RedirectResponse(url="/admin/network/vpn", status_code=303)

    except ValidationError as e:
        errors = [err["msg"] for err in e.errors()]
    except Exception as e:
        errors = [str(e)]

    vpn_defaults = _get_vpn_defaults(db)
    return templates.TemplateResponse(
        "admin/network/vpn/server_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": None,
            "errors": errors,
            "vpn_defaults": vpn_defaults,
            "form_data": {
                "name": name,
                "description": description,
                "listen_port": listen_port,
                "public_host": public_host,
                "public_port": public_port,
                "vpn_address": vpn_address,
                "mtu": mtu,
                "dns_servers": dns_servers,
                "is_active": is_active,
                "interface_name": interface_name,
                "auto_deploy": auto_deploy,
                "router_enabled": router_enabled,
                "router_host": router_host,
                "router_api_port": router_api_port,
                "router_username": router_username,
                "router_interface_name": router_interface_name,
                "router_api_ssl": router_api_ssl,
            },
        },
    )


@router.get("/servers/{server_id}/edit", response_class=HTMLResponse)
async def server_form_edit(server_id: UUID, request: Request, db: Session = Depends(get_db)):
    """Edit WireGuard server form."""
    server = wg_service.wg_servers.get(db, server_id)
    vpn_defaults = _get_vpn_defaults(db)

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
async def server_update(
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
    errors = []
    server_before = wg_service.wg_servers.get(db, server_id)
    before_snapshot = model_to_dict(server_before, exclude=SERVER_AUDIT_EXCLUDE_FIELDS)
    vpn_defaults = _get_vpn_defaults(db)

    # Validate router config
    if router_enabled and not router_host:
        errors.append("Router host is required when router sync is enabled")

    # Parse DNS servers
    dns_list = None
    if dns_servers:
        dns_list = [s.strip() for s in dns_servers.split(",") if s.strip()]
    vpn_address = (vpn_address or "").strip()
    if not vpn_address:
        vpn_address = server_before.vpn_address or vpn_defaults.get("vpn_address") or "10.10.0.1/24"
    vpn_address_v6 = (vpn_address_v6 or "").strip() or None

    routes_list, route_errors = _parse_route_cidrs(vpn_routes)
    errors.extend(route_errors)

    if errors:
        return templates.TemplateResponse(
            "admin/network/vpn/server_form.html",
            {
                **_base_context(request, db, "vpn"),
                "server": server_before,
                "errors": errors,
                "vpn_defaults": vpn_defaults,
                "form_data": {
                    "name": name,
                    "description": description,
                    "listen_port": listen_port,
                    "public_host": public_host,
                    "public_port": public_port,
                    "vpn_address": vpn_address,
                    "vpn_address_v6": vpn_address_v6,
                    "mtu": mtu,
                    "dns_servers": dns_servers,
                    "vpn_routes": vpn_routes,
                    "is_active": is_active,
                    "interface_name": interface_name,
                    "auto_deploy": auto_deploy,
                    "router_enabled": router_enabled,
                    "router_host": router_host,
                    "router_api_port": router_api_port,
                    "router_username": router_username,
                    "router_interface_name": router_interface_name,
                    "router_api_ssl": router_api_ssl,
                },
            },
        )

    try:
        payload = WireGuardServerUpdate(
            name=name,
            description=description or None,
            listen_port=listen_port,
            public_host=public_host or None,
            public_port=public_port,
            vpn_address=vpn_address,
            vpn_address_v6=vpn_address_v6,
            mtu=mtu,
            dns_servers=dns_list,
            is_active=is_active,
            interface_name=interface_name,
        )
        server = wg_service.wg_servers.update(db, server_id, payload)
        after_snapshot = model_to_dict(server, exclude=SERVER_AUDIT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None

        # Update auto_deploy and router config in metadata
        server.metadata_ = server.metadata_ or {}
        server.metadata_["auto_deploy"] = auto_deploy
        if routes_list:
            server.metadata_["routes"] = routes_list
        else:
            server.metadata_.pop("routes", None)

        # Handle router configuration
        if router_enabled and router_host:
            existing_router = server.metadata_.get("router", {})
            router_config = {
                "enabled": True,
                "host": router_host,
                "api_port": router_api_port,
                "username": router_username or "",
                "interface_name": router_interface_name or "wg-infra",
                "api_ssl": router_api_ssl,
            }
            # Preserve existing password if not provided, otherwise encrypt new one
            if router_password:
                router_config["password"] = encrypt_private_key(router_password)
            elif existing_router.get("password"):
                router_config["password"] = existing_router["password"]
            server.metadata_["router"] = router_config
        elif not router_enabled and "router" in server.metadata_:
            # Disable router sync but preserve config
            server.metadata_["router"]["enabled"] = False
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(server, "metadata_")

        db.commit()
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="wireguard_server",
            entity_id=str(server.id),
            actor_id=str(current_user.get("person_id")) if current_user else None,
            metadata=metadata,
        )

        # Deploy WireGuard interface if auto_deploy is enabled
        if auto_deploy and server.public_key:
            try:
                from app.services.wireguard_system import WireGuardSystemService
                WireGuardSystemService.deploy_server(db, server.id)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Auto-deploy failed: {e}")

        return RedirectResponse(url="/admin/network/vpn", status_code=303)

    except ValidationError as e:
        errors = [err["msg"] for err in e.errors()]
    except Exception as e:
        errors = [str(e)]

    server = wg_service.wg_servers.get(db, server_id)
    vpn_defaults = _get_vpn_defaults(db)

    return templates.TemplateResponse(
        "admin/network/vpn/server_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "errors": errors,
            "vpn_defaults": vpn_defaults,
        },
    )


@router.post("/servers/{server_id}/regenerate-keys")
async def server_regenerate_keys(
    request: Request,
    server_id: UUID,
    db: Session = Depends(get_db),
):
    """Regenerate server keypair."""
    wg_service.wg_servers.regenerate_keys(db, server_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="regenerate_keys",
        entity_type="wireguard_server",
        entity_id=str(server_id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata=None,
    )
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


@router.post("/servers/{server_id}/deploy")
async def server_deploy(request: Request, server_id: UUID, db: Session = Depends(get_db)):
    """Deploy WireGuard server configuration to the system."""
    from app.services.wireguard_system import WireGuardSystemService

    success, message = WireGuardSystemService.deploy_server(db, server_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="deploy",
        entity_type="wireguard_server",
        entity_id=str(server_id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata={"success": success, "message": message},
    )
    # TODO: Flash message with result
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


@router.post("/servers/{server_id}/undeploy")
async def server_undeploy(request: Request, server_id: UUID, db: Session = Depends(get_db)):
    """Bring down WireGuard interface."""
    from app.services.wireguard_system import WireGuardSystemService

    success, message = WireGuardSystemService.undeploy_server(db, server_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="undeploy",
        entity_type="wireguard_server",
        entity_id=str(server_id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata={"success": success, "message": message},
    )
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


@router.post("/servers/{server_id}/test-router")
async def server_test_router_connection(
    request: Request,
    server_id: UUID,
    db: Session = Depends(get_db),
):
    """Test MikroTik router API connection for a server."""
    server = wg_service.wg_servers.get(db, server_id)
    if not server:
        return JSONResponse(
            {"success": False, "message": "Server not found"},
            status_code=404,
        )

    router_config = (server.metadata_ or {}).get("router", {})
    if not router_config.get("enabled") or not router_config.get("host"):
        return JSONResponse(
            {"success": False, "message": "Router sync not configured for this server"},
            status_code=400,
        )

    # Decrypt password if stored
    password = ""
    if router_config.get("password"):
        try:
            from app.services.wireguard_crypto import decrypt_private_key
            password = decrypt_private_key(router_config["password"])
        except Exception:
            return JSONResponse(
                {"success": False, "message": "Failed to decrypt stored password"},
                status_code=500,
            )

    # Test connection
    try:
        from routeros_api import RouterOsApiPool

        host = router_config["host"]
        port = router_config.get("api_port", 8728)
        username = router_config.get("username", "admin")
        use_ssl = router_config.get("api_ssl", False)

        # Create connection pool and test
        pool = RouterOsApiPool(
            host=host,
            username=username,
            password=password,
            port=port,
            use_ssl=use_ssl,
            ssl_verify=False,
            plaintext_login=True,
        )
        api = pool.get_api()

        # Try to get router identity as a simple test
        identity = api.get_resource("/system/identity").get()
        router_name = identity[0].get("name", "Unknown") if identity else "Unknown"

        # Check if WireGuard interface exists
        wg_interface = router_config.get("interface_name", "wg-infra")
        interfaces = api.get_resource("/interface/wireguard").get()
        interface_exists = any(iface.get("name") == wg_interface for iface in interfaces)

        pool.disconnect()

        return JSONResponse({
            "success": True,
            "message": f"Connected to router '{router_name}'",
            "router_name": router_name,
            "interface_exists": interface_exists,
            "interface_name": wg_interface,
        })

    except ImportError:
        return JSONResponse(
            {"success": False, "message": "RouterOS API library not installed"},
            status_code=500,
        )
    except Exception as e:
        error_msg = str(e)
        if "Connection refused" in error_msg:
            error_msg = "Connection refused - check host and port"
        elif "timed out" in error_msg.lower():
            error_msg = "Connection timed out - check network connectivity"
        elif "authentication" in error_msg.lower() or "login" in error_msg.lower():
            error_msg = "Authentication failed - check username and password"

        return JSONResponse(
            {"success": False, "message": f"Connection failed: {error_msg}"},
            status_code=400,
        )


@router.post("/servers/{server_id}/delete")
async def server_delete(request: Request, server_id: UUID, db: Session = Depends(get_db)):
    """Delete WireGuard server."""
    from app.services.wireguard_system import WireGuardSystemService

    # Undeploy first if interface is up
    server = wg_service.wg_servers.get(db, server_id)
    if server:
        WireGuardSystemService.undeploy_server(db, server_id)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="wireguard_server",
            entity_id=str(server_id),
            actor_id=str(current_user.get("person_id")) if current_user else None,
            metadata={"name": server.name},
        )

    wg_service.wg_servers.delete(db, server_id)
    return RedirectResponse(url="/admin/network/vpn", status_code=303)


# ============== Peer Routes ==============


@router.get("/servers/{server_id}/peers/new", response_class=HTMLResponse)
async def peer_form_new(server_id: UUID, request: Request, db: Session = Depends(get_db)):
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
async def peer_create(
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
    errors = []
    server = wg_service.wg_servers.get(db, server_id)

    known_subnets_list, subnet_errors = _parse_known_subnets(known_subnets)
    errors.extend(subnet_errors)

    lan_subnets_list, lan_subnet_errors = _parse_lan_subnets(lan_subnets)
    errors.extend(lan_subnet_errors)

    peer_address_v6 = (peer_address_v6 or "").strip() or None
    if errors:
        return templates.TemplateResponse(
            "admin/network/vpn/peer_form.html",
            {
                **_base_context(request, db, "vpn"),
                "server": server,
                "peer": None,
                "errors": errors,
                "form_data": {
                    "name": name,
                    "description": description,
                    "peer_address": peer_address,
                    "peer_address_v6": peer_address_v6,
                    "persistent_keepalive": persistent_keepalive,
                    "known_subnets": known_subnets,
                    "lan_subnets": lan_subnets,
                    "notes": notes,
                },
                            },
        )

    try:
        metadata = {}
        if known_subnets_list:
            metadata["known_subnets"] = known_subnets_list
        if lan_subnets_list:
            metadata["lan_subnets"] = lan_subnets_list
        if not metadata:
            metadata = None
        payload = WireGuardPeerCreate(
            server_id=server_id,
            name=name,
            description=description or None,
            peer_address=peer_address or None,
            peer_address_v6=peer_address_v6,
            persistent_keepalive=persistent_keepalive,
            use_preshared_key=use_preshared_key,
            notes=notes or None,
            metadata_=metadata,
        )
        created = wg_service.wg_peers.create(db, payload)

        # Sync LAN subnets to allowed_ips and server routes if provided
        if lan_subnets_list:
            from app.services.vpn_routing import sync_lan_subnets
            peer = wg_service.wg_peers.get(db, created.id)
            sync_lan_subnets(peer, server)
            db.commit()
            # Redeploy to apply the new routes
            from app.services.wireguard_system import WireGuardSystemService
            WireGuardSystemService.deploy_server(db, server.id)

        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="wireguard_peer",
            entity_id=str(created.id),
            actor_id=str(current_user.get("person_id")) if current_user else None,
            metadata={"name": created.name},
        )

        # Redirect to peer detail to show the private key
        return RedirectResponse(
            url=f"/admin/network/vpn/peers/{created.id}?show_keys=true",
            status_code=303,
        )

    except ValidationError as e:
        errors = [err["msg"] for err in e.errors()]
    except Exception as e:
        errors = [str(e)]

    return templates.TemplateResponse(
        "admin/network/vpn/peer_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": None,
            "errors": errors,
            "form_data": {
                "name": name,
                "description": description,
                "peer_address": peer_address,
                "peer_address_v6": peer_address_v6,
                "persistent_keepalive": persistent_keepalive,
                "known_subnets": known_subnets,
                "notes": notes,
            },
                    },
    )


@router.get("/peers/{peer_id}", response_class=HTMLResponse)
async def peer_detail(
    peer_id: UUID,
    request: Request,
    show_keys: bool = False,
    db: Session = Depends(get_db),
):
    """WireGuard peer detail page."""
    peer = wg_service.wg_peers.get(db, peer_id)
    server = wg_service.wg_servers.get(db, peer.server_id)

    interface_status = None
    if server.public_key:
        from app.services.wireguard_system import WireGuardSystemService
        interface_status = WireGuardSystemService.get_interface_status(server.interface_name)
        _sync_peer_stats_from_interface(db, server, interface_status)
        peer = wg_service.wg_peers.get(db, peer_id)

    peer_read = wg_service.wg_peers.to_read_schema(peer, db)

    # Get connection logs
    logs = wg_service.wg_connection_logs.list_by_peer(db, peer_id, limit=20)

    # Check for recent connection
    now = datetime.now(timezone.utc)
    is_connected = False
    if peer.last_handshake_at:
        is_connected = (now - peer.last_handshake_at).total_seconds() < 180

    # Get MikroTik script if private key is stored
    mikrotik_script = None
    peer_config = None
    if peer.private_key:
        try:
            mikrotik_script = wg_service.wg_mikrotik.generate_script(db, peer_id)
            peer_config = wg_service.wg_peers.generate_peer_config(db, peer_id)
        except Exception:
            pass

    activities = _build_audit_activities(db, "wireguard_peer", str(peer.id), limit=10)

    return templates.TemplateResponse(
        "admin/network/vpn/peer_detail.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": peer_read,
            "peer_model": peer,
            "is_connected": is_connected,
            "connection_logs": logs,
            "mikrotik_script": mikrotik_script,
            "peer_config": peer_config,
            "show_keys": show_keys,
            "activities": activities,
        },
    )


@router.get("/peers/{peer_id}/edit", response_class=HTMLResponse)
async def peer_form_edit(peer_id: UUID, request: Request, db: Session = Depends(get_db)):
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
async def peer_update(
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
    errors = []
    peer = wg_service.wg_peers.get(db, peer_id)
    server = wg_service.wg_servers.get(db, peer.server_id)
    before_snapshot = model_to_dict(peer, exclude=PEER_AUDIT_EXCLUDE_FIELDS)

    known_subnets_list, subnet_errors = _parse_known_subnets(known_subnets)
    errors.extend(subnet_errors)

    lan_subnets_list, lan_subnet_errors = _parse_lan_subnets(lan_subnets)
    errors.extend(lan_subnet_errors)

    peer_address_v6 = (peer_address_v6 or "").strip() or None
    if errors:
        return templates.TemplateResponse(
            "admin/network/vpn/peer_form.html",
            {
                **_base_context(request, db, "vpn"),
                "server": server,
                "peer": peer,
                "errors": errors,
                "form_data": {
                    "name": name,
                    "description": description,
                    "peer_address": peer_address,
                    "peer_address_v6": peer_address_v6,
                    "persistent_keepalive": persistent_keepalive,
                    "known_subnets": known_subnets,
                    "lan_subnets": lan_subnets,
                    "notes": notes,
                },
                            },
        )

    try:
        previous_lan_subnets = []
        if peer.metadata_:
            previous_lan_subnets = peer.metadata_.get("lan_subnets") or []
        metadata = dict(peer.metadata_ or {})
        if known_subnets_list:
            metadata["known_subnets"] = known_subnets_list
        else:
            metadata.pop("known_subnets", None)
        if lan_subnets_list:
            metadata["lan_subnets"] = lan_subnets_list
        else:
            metadata.pop("lan_subnets", None)
        if not metadata:
            metadata = None

        payload = WireGuardPeerUpdate(
            name=name,
            description=description or None,
            peer_address=peer_address or None,
            peer_address_v6=peer_address_v6,
            persistent_keepalive=persistent_keepalive,
            status=WireGuardPeerStatus(status),
            notes=notes or None,
            metadata_=metadata,
        )
        updated_peer = wg_service.wg_peers.update(db, peer_id, payload)

        # Sync LAN subnets to allowed_ips and server routes
        from app.services.vpn_routing import sync_lan_subnets
        sync_lan_subnets(updated_peer, server, previous_lan_subnets)
        db.commit()
        # Ensure server config is redeployed to apply route changes.
        from app.services.wireguard_system import WireGuardSystemService
        WireGuardSystemService.deploy_server(db, server.id)

        after_snapshot = model_to_dict(updated_peer, exclude=PEER_AUDIT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        audit_metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="wireguard_peer",
            entity_id=str(updated_peer.id),
            actor_id=str(current_user.get("person_id")) if current_user else None,
            metadata=audit_metadata,
        )
        return RedirectResponse(
            url=f"/admin/network/vpn?server_id={server.id}&success=Peer+updated+successfully",
            status_code=303,
        )

    except ValidationError as e:
        errors = [err["msg"] for err in e.errors()]
    except Exception as e:
        errors = [str(e)]

    return templates.TemplateResponse(
        "admin/network/vpn/peer_form.html",
        {
            **_base_context(request, db, "vpn"),
            "server": server,
            "peer": peer,
            "errors": errors,
                    },
    )


@router.post("/peers/{peer_id}/disable")
async def peer_disable(request: Request, peer_id: UUID, db: Session = Depends(get_db)):
    """Disable WireGuard peer."""
    peer = wg_service.wg_peers.disable(db, peer_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="disable",
        entity_type="wireguard_peer",
        entity_id=str(peer.id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata={"status": peer.status.value if peer.status else None},
    )
    return RedirectResponse(
        url=f"/admin/network/vpn?server_id={peer.server_id}",
        status_code=303,
    )


@router.post("/peers/{peer_id}/enable")
async def peer_enable(request: Request, peer_id: UUID, db: Session = Depends(get_db)):
    """Enable WireGuard peer."""
    peer = wg_service.wg_peers.enable(db, peer_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="enable",
        entity_type="wireguard_peer",
        entity_id=str(peer.id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata={"status": peer.status.value if peer.status else None},
    )
    return RedirectResponse(
        url=f"/admin/network/vpn?server_id={peer.server_id}",
        status_code=303,
    )


@router.post("/peers/{peer_id}/delete")
async def peer_delete(request: Request, peer_id: UUID, db: Session = Depends(get_db)):
    """Delete WireGuard peer."""
    peer = wg_service.wg_peers.get(db, peer_id)
    server_id = peer.server_id
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="wireguard_peer",
        entity_id=str(peer.id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata={"name": peer.name},
    )
    wg_service.wg_peers.delete(db, peer_id)
    return RedirectResponse(
        url=f"/admin/network/vpn?server_id={server_id}",
        status_code=303,
    )


@router.post("/peers/{peer_id}/regenerate-token")
async def peer_regenerate_token(
    request: Request,
    peer_id: UUID,
    db: Session = Depends(get_db),
):
    """Regenerate provisioning token for a peer."""
    wg_service.wg_peers.regenerate_provision_token(db, peer_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="regenerate_token",
        entity_type="wireguard_peer",
        entity_id=str(peer_id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata=None,
    )
    return RedirectResponse(
        url=f"/admin/network/vpn/peers/{peer_id}",
        status_code=303,
    )
