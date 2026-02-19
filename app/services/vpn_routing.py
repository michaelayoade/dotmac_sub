"""Helpers for ensuring VPN interfaces are ready for device access."""

from __future__ import annotations

import os
from ipaddress import ip_address, ip_network
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.wireguard import WireGuardPeer, WireGuardServer
from app.services.wireguard_system import WireGuardSystemService


class VpnRoutingError(RuntimeError):
    """Raised when a VPN interface is unavailable for device access."""


def _get_blocked_lan_subnet_networks() -> list:
    raw = os.getenv("VPN_BLOCKED_LAN_SUBNETS") or os.getenv("DOCKER_NETWORK_CIDRS")
    cidrs = []
    if raw:
        cidrs.extend([item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()])
    if not cidrs:
        cidrs = ["172.20.0.0/16"]
    blocked = []
    for cidr in cidrs:
        try:
            blocked.append(ip_network(cidr, strict=False))
        except ValueError:
            continue
    return blocked


def _is_blocked_lan_subnet(network) -> bool:
    for blocked in _get_blocked_lan_subnet_networks():
        if network.overlaps(blocked):
            return True
    return False

def ensure_vpn_ready(
    db: Session, wireguard_server_id: str | UUID | None
) -> WireGuardServer | None:
    if not wireguard_server_id:
        return None

    server = (
        db.query(WireGuardServer)
        .filter(WireGuardServer.id == wireguard_server_id)
        .first()
    )
    if not server:
        raise VpnRoutingError("Selected VPN was not found.")

    if not server.is_active:
        raise VpnRoutingError(f"Selected VPN '{server.name}' is inactive.")

    if WireGuardSystemService.is_interface_up(server.interface_name):
        return server

    success, message = WireGuardSystemService.deploy_server(db, server.id)
    if not success:
        raise VpnRoutingError(
            f"VPN interface '{server.interface_name}' is unavailable: {message}"
        )

    return server


def sync_peer_routes_for_ip(
    peer: WireGuardPeer,
    server: WireGuardServer,
    mgmt_ip: str | None,
) -> bool:
    if not mgmt_ip:
        return False

    try:
        ip = ip_address(mgmt_ip)
    except ValueError:
        return False

    if not ip.is_private:
        return False

    known_subnets: list[str] = []
    if peer.metadata_:
        raw_known = peer.metadata_.get("known_subnets")
        if isinstance(raw_known, list):
            known_subnets = [str(item) for item in raw_known if item]

    known_networks = _normalize_networks(known_subnets)
    target_cidr = _select_target_cidr(ip, known_networks)

    changed = False
    allowed_ips = list(peer.allowed_ips or [])
    if not _ip_in_networks(ip, allowed_ips):
        if target_cidr not in allowed_ips:
            allowed_ips.append(target_cidr)
            peer.allowed_ips = allowed_ips
            changed = True

    server.metadata_ = server.metadata_ or {}
    routes = list(server.metadata_.get("routes") or [])
    if target_cidr not in routes:
        routes.append(target_cidr)
        server.metadata_["routes"] = routes
        changed = True

    return changed


def _normalize_networks(networks: list[str]) -> list:
    normalized = []
    for cidr in networks:
        try:
            normalized.append(ip_network(cidr, strict=False))
        except ValueError:
            continue
    return normalized


def _select_target_cidr(ip, networks: list) -> str:
    matching = [net for net in networks if ip in net]
    if matching:
        matching.sort(key=lambda net: net.prefixlen, reverse=True)
        return str(matching[0])
    suffix = "128" if ip.version == 6 else "32"
    return f"{ip}/{suffix}"


def _ip_in_networks(ip, networks: list[str]) -> bool:
    for cidr in networks:
        try:
            if ip in ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def sync_lan_subnets(
    peer: WireGuardPeer,
    server: WireGuardServer,
    previous_subnets: list[str] | None = None,
) -> bool:
    """Sync LAN subnets from peer metadata to allowed_ips and server routes.

    This enables routing to networks behind the peer (e.g., 172.16.10.0/24).

    Reads from peer.metadata_["lan_subnets"] and:
    1. Adds subnets to peer.allowed_ips (tells WireGuard to route via this peer)
    2. Adds subnets to server.metadata_["routes"] (adds system routes)

    Args:
        peer: WireGuard peer with lan_subnets in metadata
        server: WireGuard server to update routes on

    Returns:
        True if any configuration changed
    """
    lan_subnets: list[str] = []
    if peer.metadata_:
        raw_lan = peer.metadata_.get("lan_subnets")
        if isinstance(raw_lan, list):
            lan_subnets = [str(item) for item in raw_lan if item]

    previous_subnets = previous_subnets or []

    # Normalize and validate subnets
    valid_subnets = []
    for cidr in lan_subnets:
        try:
            net = ip_network(cidr, strict=False)
            if _is_blocked_lan_subnet(net):
                continue
            valid_subnets.append(str(net))
        except ValueError:
            continue

    previous_normalized = []
    for cidr in previous_subnets:
        try:
            net = ip_network(cidr, strict=False)
            previous_normalized.append(str(net))
        except ValueError:
            continue

    if not valid_subnets and not previous_normalized:
        return False

    changed = False

    # Remove stale subnets
    allowed_ips = list(peer.allowed_ips or [])
    for subnet in previous_normalized:
        if subnet not in valid_subnets and subnet in allowed_ips:
            allowed_ips.remove(subnet)
            changed = True

    # Add to peer's allowed_ips
    for subnet in valid_subnets:
        if subnet not in allowed_ips:
            allowed_ips.append(subnet)
            changed = True

    if changed:
        peer.allowed_ips = allowed_ips
        flag_modified(peer, "allowed_ips")

    # Add to server routes
    server.metadata_ = server.metadata_ or {}
    routes = list(server.metadata_.get("routes") or [])
    for subnet in previous_normalized:
        if subnet not in valid_subnets and subnet in routes:
            routes.remove(subnet)
            changed = True
    for subnet in valid_subnets:
        if subnet not in routes:
            routes.append(subnet)
            changed = True

    if routes != (server.metadata_.get("routes") or []):
        server.metadata_["routes"] = routes
        flag_modified(server, "metadata_")

    return changed


def configure_peer_lan_routing(
    db: Session,
    peer: WireGuardPeer,
    lan_subnets: list[str],
    deploy: bool = True,
) -> tuple[bool, str]:
    """Configure LAN subnet routing for a peer and optionally deploy.

    This is a convenience function that:
    1. Sets peer.metadata_["lan_subnets"]
    2. Syncs to peer.allowed_ips and server.metadata_["routes"]
    3. Optionally deploys the updated WireGuard config

    Args:
        db: Database session
        peer: WireGuard peer to configure
        lan_subnets: List of CIDR strings (e.g., ["172.16.10.0/24"])
        deploy: Whether to deploy the config after updating

    Returns:
        Tuple of (success, message)

    Example:
        # Router at 10.10.0.5 has 172.16.10.0/24 behind it
        configure_peer_lan_routing(db, peer, ["172.16.10.0/24"])
    """
    from app.services.wireguard_system import WireGuardSystemService

    # Validate subnets
    valid_subnets = []
    for cidr in lan_subnets:
        try:
            net = ip_network(cidr, strict=False)
            valid_subnets.append(str(net))
        except ValueError:
            return False, f"Invalid CIDR: {cidr}"

    # Update peer metadata
    previous_subnets: list[str] = []
    if peer.metadata_:
        raw_previous = peer.metadata_.get("lan_subnets")
        if isinstance(raw_previous, list):
            previous_subnets = [str(item) for item in raw_previous if item]
    peer.metadata_ = peer.metadata_ or {}
    peer.metadata_["lan_subnets"] = valid_subnets

    # Get server
    server = (
        db.query(WireGuardServer)
        .filter(WireGuardServer.id == peer.server_id)
        .first()
    )
    if not server:
        return False, "Server not found"

    # Sync to allowed_ips and server routes
    sync_lan_subnets(peer, server, previous_subnets)

    db.commit()

    if deploy:
        success, msg = WireGuardSystemService.deploy_server(db, server.id)
        if not success:
            return False, f"Config updated but deploy failed: {msg}"
        return True, f"Configured LAN routing for {valid_subnets} and deployed"

    return True, f"Configured LAN routing for {valid_subnets} (not deployed)"
