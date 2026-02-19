"""Service helpers for admin WireGuard server web routes."""

from __future__ import annotations

import ipaddress
import logging
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.wireguard import WireGuardServer
from app.schemas.wireguard import WireGuardServerCreate, WireGuardServerUpdate
from app.services.audit_helpers import diff_dicts, log_audit_event, model_to_dict
from app.services.wireguard_crypto import encrypt_private_key

logger = logging.getLogger(__name__)

SERVER_AUDIT_EXCLUDE_FIELDS = {"private_key", "metadata_"}


def validate_router_config(router_enabled: bool, router_host: str | None) -> list[str]:
    """Validate router sync config."""
    errors = []
    if router_enabled and not router_host:
        errors.append("Router host is required when router sync is enabled")
    return errors


def parse_dns_servers(dns_servers: str | None) -> list[str] | None:
    """Normalize comma-separated DNS servers."""
    if not dns_servers:
        return None
    parsed = [s.strip() for s in dns_servers.split(",") if s.strip()]
    return parsed or None


def normalize_vpn_addresses(
    vpn_address: str | None,
    vpn_address_v6: str | None,
    *,
    default_vpn_address: str,
) -> tuple[str, str | None]:
    """Normalize VPN address values with fallback."""
    address = (vpn_address or "").strip() or default_vpn_address
    address_v6 = (vpn_address_v6 or "").strip() or None
    return address, address_v6


def build_form_data(**kwargs) -> dict[str, object]:
    """Build template-friendly form_data dict."""
    return dict(kwargs)


def create_payload(
    *,
    name: str,
    description: str | None,
    listen_port: int,
    public_host: str | None,
    public_port: int | None,
    vpn_address: str,
    vpn_address_v6: str | None,
    mtu: int,
    dns_list: list[str] | None,
    is_active: bool,
    interface_name: str,
) -> WireGuardServerCreate:
    """Build server create payload."""
    return WireGuardServerCreate(
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


def update_payload(
    *,
    name: str,
    description: str | None,
    listen_port: int,
    public_host: str | None,
    public_port: int | None,
    vpn_address: str,
    vpn_address_v6: str | None,
    mtu: int,
    dns_list: list[str] | None,
    is_active: bool,
    interface_name: str,
) -> WireGuardServerUpdate:
    """Build server update payload."""
    return WireGuardServerUpdate(
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


def apply_metadata(
    server,
    *,
    auto_deploy: bool,
    routes_list: list[str],
    router_enabled: bool,
    router_host: str | None,
    router_api_port: int,
    router_username: str | None,
    router_password: str | None,
    router_interface_name: str | None,
    router_api_ssl: bool,
    preserve_existing_router_password: bool,
) -> None:
    """Apply/update metadata fields for server route settings."""
    server.metadata_ = server.metadata_ or {}
    server.metadata_["auto_deploy"] = auto_deploy
    if routes_list:
        server.metadata_["routes"] = routes_list
    else:
        server.metadata_.pop("routes", None)

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
        if router_password:
            router_config["password"] = encrypt_private_key(router_password)
        elif preserve_existing_router_password and existing_router.get("password"):
            router_config["password"] = existing_router["password"]
        server.metadata_["router"] = router_config
    elif not router_enabled and "router" in server.metadata_:
        server.metadata_["router"]["enabled"] = False

    flag_modified(server, "metadata_")


def maybe_deploy_server(db: Session, server: WireGuardServer, *, auto_deploy: bool) -> None:
    """Deploy server when auto_deploy is enabled and keys exist."""
    if not auto_deploy or not server.public_key:
        return
    try:
        from app.services.wireguard_system import WireGuardSystemService

        WireGuardSystemService.deploy_server(db, server.id)
    except Exception:
        logger.warning("Auto-deploy failed", exc_info=True)


def get_vpn_defaults(db: Session) -> dict[str, object]:
    """Get VPN default settings from domain settings."""
    defaults: dict[str, object] = {
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
        stmt = (
            select(DomainSetting)
            .where(DomainSetting.domain == SettingDomain.network)
            .where(DomainSetting.key == setting_key)
            .where(DomainSetting.is_active.is_(True))
        )
        setting = db.scalars(stmt).first()
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


def parse_route_cidrs(route_text: str | None) -> tuple[list[str], list[str]]:
    """Parse and validate CIDR route strings.

    Returns:
        Tuple of (normalized_routes, errors).
    """
    if not route_text:
        return [], []

    raw_items = [item.strip() for item in route_text.replace("\n", ",").split(",")]
    routes = [item for item in raw_items if item]
    errors: list[str] = []
    normalized: list[str] = []
    for cidr in routes:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            normalized.append(str(network))
        except ValueError:
            errors.append(f"Invalid route CIDR: {cidr}")
    return normalized, errors


def build_dashboard_data(
    db: Session,
    server_id: str | None = None,
) -> dict[str, object]:
    """Build VPN dashboard context data.

    Returns a dict with keys:
        all_servers, server, needs_setup, peers_read,
        interface_status, servers_with_counts.
    """
    from app.services import wireguard as wg_service
    from app.services.wireguard_system import WireGuardSystemService

    all_servers = wg_service.wg_servers.list(db, limit=50)

    if not all_servers:
        return {
            "all_servers": [],
            "server": None,
            "needs_setup": True,
            "peers_read": [],
            "interface_status": None,
            "servers_with_counts": [],
        }

    if server_id:
        server = wg_service.wg_servers.get(db, server_id)
    else:
        server = all_servers[0]

    needs_setup = not server.public_key

    interface_status = None
    if server.public_key:
        interface_status = WireGuardSystemService.get_interface_status(
            server.interface_name,
        )

    sync_peer_stats_from_interface(db, server, interface_status)
    peers = wg_service.wg_peers.list(db, server_id=server.id, limit=200)
    peers_read = [wg_service.wg_peers.to_read_schema(p, db) for p in peers]

    servers_with_counts: list[dict[str, object]] = []
    for s in all_servers:
        peer_count = wg_service.wg_servers.get_peer_count(db, s.id)
        servers_with_counts.append({
            "server": s,
            "peer_count": peer_count,
            "is_selected": str(s.id) == str(server.id),
            "needs_setup": not s.public_key,
        })

    return {
        "all_servers": all_servers,
        "server": server,
        "needs_setup": needs_setup,
        "peers_read": peers_read,
        "interface_status": interface_status,
        "servers_with_counts": servers_with_counts,
    }


def create_server(
    db: Session,
    *,
    name: str,
    description: str | None,
    listen_port: int,
    public_host: str | None,
    public_port: int | None,
    vpn_address: str,
    vpn_address_v6: str | None,
    mtu: int,
    dns_servers: str | None,
    vpn_routes: str | None,
    is_active: bool,
    interface_name: str,
    auto_deploy: bool,
    router_enabled: bool,
    router_host: str | None,
    router_api_port: int,
    router_username: str | None,
    router_password: str | None,
    router_interface_name: str,
    router_api_ssl: bool,
    actor_id: str | None,
    request: object,
) -> tuple[WireGuardServer | None, list[str]]:
    """Validate, create a server, apply metadata, audit and deploy.

    Returns:
        Tuple of (server_or_none, errors).  Empty errors means success.
    """
    from app.services import wireguard as wg_service

    errors = validate_router_config(router_enabled, router_host)
    vpn_defaults = get_vpn_defaults(db)
    dns_list = parse_dns_servers(dns_servers)
    vpn_address, vpn_address_v6 = normalize_vpn_addresses(
        vpn_address,
        vpn_address_v6,
        default_vpn_address=str(vpn_defaults.get("vpn_address") or "10.10.0.1/24"),
    )

    routes_list, route_errors = parse_route_cidrs(vpn_routes)
    errors.extend(route_errors)

    if errors:
        return None, errors

    try:
        payload = create_payload(
            name=name,
            description=description,
            listen_port=listen_port,
            public_host=public_host,
            public_port=public_port,
            vpn_address=vpn_address,
            vpn_address_v6=vpn_address_v6,
            mtu=mtu,
            dns_list=dns_list,
            is_active=is_active,
            interface_name=interface_name,
        )
        server = wg_service.wg_servers.create(db, payload)
        apply_metadata(
            server,
            auto_deploy=auto_deploy,
            routes_list=routes_list,
            router_enabled=router_enabled,
            router_host=router_host,
            router_api_port=router_api_port,
            router_username=router_username,
            router_password=router_password,
            router_interface_name=router_interface_name,
            router_api_ssl=router_api_ssl,
            preserve_existing_router_password=False,
        )

        db.commit()
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="wireguard_server",
            entity_id=str(server.id),
            actor_id=actor_id,
            metadata={"name": server.name},
        )

        maybe_deploy_server(db, server, auto_deploy=auto_deploy)

        return server, []

    except ValidationError as e:
        return None, [err["msg"] for err in e.errors()]
    except Exception as e:
        return None, [str(e)]


def update_server(
    db: Session,
    server_id: UUID,
    *,
    name: str,
    description: str | None,
    listen_port: int,
    public_host: str | None,
    public_port: int | None,
    vpn_address: str,
    vpn_address_v6: str | None,
    mtu: int,
    dns_servers: str | None,
    vpn_routes: str | None,
    is_active: bool,
    interface_name: str,
    auto_deploy: bool,
    router_enabled: bool,
    router_host: str | None,
    router_api_port: int,
    router_username: str | None,
    router_password: str | None,
    router_interface_name: str,
    router_api_ssl: bool,
    actor_id: str | None,
    request: object,
) -> tuple[WireGuardServer | None, list[str]]:
    """Validate, update a server, apply metadata, audit and deploy.

    Returns:
        Tuple of (server_or_none, errors).  Empty errors means success.
    """
    from app.services import wireguard as wg_service

    errors = validate_router_config(router_enabled, router_host)
    server_before = wg_service.wg_servers.get(db, server_id)
    before_snapshot = model_to_dict(server_before, exclude=SERVER_AUDIT_EXCLUDE_FIELDS)
    vpn_defaults = get_vpn_defaults(db)
    dns_list = parse_dns_servers(dns_servers)
    vpn_address, vpn_address_v6 = normalize_vpn_addresses(
        vpn_address,
        vpn_address_v6,
        default_vpn_address=(
            server_before.vpn_address
            or str(vpn_defaults.get("vpn_address") or "10.10.0.1/24")
        ),
    )

    routes_list, route_errors = parse_route_cidrs(vpn_routes)
    errors.extend(route_errors)

    if errors:
        return server_before, errors

    try:
        payload = update_payload(
            name=name,
            description=description,
            listen_port=listen_port,
            public_host=public_host,
            public_port=public_port,
            vpn_address=vpn_address,
            vpn_address_v6=vpn_address_v6,
            mtu=mtu,
            dns_list=dns_list,
            is_active=is_active,
            interface_name=interface_name,
        )
        server = wg_service.wg_servers.update(db, server_id, payload)
        after_snapshot = model_to_dict(server, exclude=SERVER_AUDIT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        audit_metadata = {"changes": changes} if changes else None

        apply_metadata(
            server,
            auto_deploy=auto_deploy,
            routes_list=routes_list,
            router_enabled=router_enabled,
            router_host=router_host,
            router_api_port=router_api_port,
            router_username=router_username,
            router_password=router_password,
            router_interface_name=router_interface_name,
            router_api_ssl=router_api_ssl,
            preserve_existing_router_password=True,
        )

        db.commit()
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="wireguard_server",
            entity_id=str(server.id),
            actor_id=actor_id,
            metadata=audit_metadata,
        )

        maybe_deploy_server(db, server, auto_deploy=auto_deploy)

        return server, []

    except ValidationError as e:
        return None, [err["msg"] for err in e.errors()]
    except Exception as e:
        return None, [str(e)]


def delete_server(
    db: Session,
    server_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> None:
    """Undeploy, audit-log, and delete a WireGuard server."""
    from app.services import wireguard as wg_service
    from app.services.wireguard_system import WireGuardSystemService

    server = wg_service.wg_servers.get(db, server_id)
    if server:
        WireGuardSystemService.undeploy_server(db, server_id)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="wireguard_server",
            entity_id=str(server_id),
            actor_id=actor_id,
            metadata={"name": server.name},
        )

    wg_service.wg_servers.delete(db, server_id)


def deploy_server(
    db: Session,
    server_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> None:
    """Deploy WireGuard server configuration and audit-log the action."""
    from app.services.wireguard_system import WireGuardSystemService

    success, message = WireGuardSystemService.deploy_server(db, server_id)
    log_audit_event(
        db=db,
        request=request,
        action="deploy",
        entity_type="wireguard_server",
        entity_id=str(server_id),
        actor_id=actor_id,
        metadata={"success": success, "message": message},
    )


def undeploy_server(
    db: Session,
    server_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> None:
    """Bring down WireGuard interface and audit-log the action."""
    from app.services.wireguard_system import WireGuardSystemService

    success, message = WireGuardSystemService.undeploy_server(db, server_id)
    log_audit_event(
        db=db,
        request=request,
        action="undeploy",
        entity_type="wireguard_server",
        entity_id=str(server_id),
        actor_id=actor_id,
        metadata={"success": success, "message": message},
    )


def regenerate_server_keys(
    db: Session,
    server_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> None:
    """Regenerate server keypair and audit-log the action."""
    from app.services import wireguard as wg_service

    wg_service.wg_servers.regenerate_keys(db, server_id)
    log_audit_event(
        db=db,
        request=request,
        action="regenerate_keys",
        entity_type="wireguard_server",
        entity_id=str(server_id),
        actor_id=actor_id,
        metadata=None,
    )


def test_router_connection(
    db: Session,
    server_id: UUID,
) -> tuple[bool, dict[str, object], int]:
    """Test MikroTik router API connection for a server.

    Returns:
        Tuple of (success, response_dict, http_status_code).
    """
    from app.services import wireguard as wg_service

    server = wg_service.wg_servers.get(db, server_id)
    if not server:
        return False, {"success": False, "message": "Server not found"}, 404

    router_config = (server.metadata_ or {}).get("router", {})
    if not router_config.get("enabled") or not router_config.get("host"):
        return (
            False,
            {"success": False, "message": "Router sync not configured for this server"},
            400,
        )

    password = ""
    if router_config.get("password"):
        try:
            from app.services.wireguard_crypto import decrypt_private_key

            password = decrypt_private_key(router_config["password"])
        except Exception:
            return (
                False,
                {"success": False, "message": "Failed to decrypt stored password"},
                500,
            )

    try:
        from routeros_api import RouterOsApiPool

        host = router_config["host"]
        port = router_config.get("api_port", 8728)
        username = router_config.get("username", "admin")
        use_ssl = router_config.get("api_ssl", False)

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

        identity = api.get_resource("/system/identity").get()
        router_name = identity[0].get("name", "Unknown") if identity else "Unknown"

        wg_interface = router_config.get("interface_name", "wg-infra")
        interfaces = api.get_resource("/interface/wireguard").get()
        interface_exists = any(
            iface.get("name") == wg_interface for iface in interfaces
        )

        pool.disconnect()

        return (
            True,
            {
                "success": True,
                "message": f"Connected to router '{router_name}'",
                "router_name": router_name,
                "interface_exists": interface_exists,
                "interface_name": wg_interface,
            },
            200,
        )

    except ImportError:
        return (
            False,
            {"success": False, "message": "RouterOS API library not installed"},
            500,
        )
    except Exception as e:
        error_msg = str(e)
        if "Connection refused" in error_msg:
            error_msg = "Connection refused - check host and port"
        elif "timed out" in error_msg.lower():
            error_msg = "Connection timed out - check network connectivity"
        elif "authentication" in error_msg.lower() or "login" in error_msg.lower():
            error_msg = "Authentication failed - check username and password"

        return (
            False,
            {"success": False, "message": f"Connection failed: {error_msg}"},
            400,
        )


def _endpoint_to_ip(endpoint: str | None) -> str | None:
    """Extract IP address from a WireGuard endpoint string."""
    if not endpoint:
        return None
    if endpoint.startswith("[") and "]" in endpoint:
        return endpoint[1 : endpoint.index("]")]
    if ":" in endpoint:
        return endpoint.rsplit(":", 1)[0]
    return endpoint


def sync_peer_stats_from_interface(
    db: Session,
    server: WireGuardServer,
    interface_status: dict[str, object] | None,
) -> None:
    """Sync peer handshake/traffic stats from a live interface status dump."""
    from datetime import UTC, datetime

    from app.services import wireguard as wg_service

    if not interface_status or not interface_status.get("is_up"):
        return
    peers_obj = interface_status.get("peers")
    if not isinstance(peers_obj, list) or not peers_obj:
        return
    peers_status: list[dict[str, object]] = [
        peer for peer in peers_obj if isinstance(peer, dict) and peer.get("public_key")
    ]
    status_by_key: dict[str, dict[str, object]] = {}
    for peer_status in peers_status:
        public_key = peer_status.get("public_key")
        if isinstance(public_key, str):
            status_by_key[public_key] = peer_status
    if not status_by_key:
        return

    peers = wg_service.wg_peers.list(db, server_id=server.id, limit=1000)
    updated = False
    for peer in peers:
        status = status_by_key.get(peer.public_key)
        if not status:
            continue
        latest_handshake = status.get("latest_handshake")
        if isinstance(latest_handshake, (int, float)) and latest_handshake:
            peer.last_handshake_at = datetime.fromtimestamp(float(latest_handshake), tz=UTC)
        endpoint = status.get("endpoint")
        endpoint_ip = _endpoint_to_ip(endpoint if isinstance(endpoint, str) else None)
        if endpoint_ip:
            peer.endpoint_ip = endpoint_ip
        rx_bytes = status.get("rx_bytes")
        if isinstance(rx_bytes, (int, float)):
            peer.rx_bytes = int(rx_bytes)
        tx_bytes = status.get("tx_bytes")
        if isinstance(tx_bytes, (int, float)):
            peer.tx_bytes = int(tx_bytes)
        updated = True

    if updated:
        db.commit()
