"""Service helpers for admin WireGuard peer web routes."""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.wireguard import WireGuardPeer, WireGuardPeerStatus, WireGuardServer
from app.schemas.wireguard import WireGuardPeerCreate, WireGuardPeerUpdate
from app.services import wireguard as wg_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)

logger = logging.getLogger(__name__)

PEER_AUDIT_EXCLUDE_FIELDS = {
    "private_key",
    "preshared_key",
    "provision_token_hash",
}


def normalize_peer_address_v6(peer_address_v6: str | None) -> str | None:
    """Normalize optional v6 peer address."""
    return (peer_address_v6 or "").strip() or None


def build_form_data(**kwargs) -> dict[str, object]:
    """Build template form_data dictionary for peer forms."""
    return dict(kwargs)


def build_metadata(
    *,
    existing_metadata: dict | None,
    known_subnets_list: list[str],
    lan_subnets_list: list[str],
) -> dict | None:
    """Build peer metadata from known/lan subnet lists."""
    metadata = dict(existing_metadata or {})
    if known_subnets_list:
        metadata["known_subnets"] = known_subnets_list
    else:
        metadata.pop("known_subnets", None)
    if lan_subnets_list:
        metadata["lan_subnets"] = lan_subnets_list
    else:
        metadata.pop("lan_subnets", None)
    return metadata or None


def create_payload(
    *,
    server_id: UUID,
    name: str,
    description: str | None,
    peer_address: str | None,
    peer_address_v6: str | None,
    persistent_keepalive: int,
    use_preshared_key: bool,
    notes: str | None,
    metadata: dict | None,
) -> WireGuardPeerCreate:
    """Build peer create payload."""
    return WireGuardPeerCreate(
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


def update_payload(
    *,
    name: str,
    description: str | None,
    peer_address: str | None,
    peer_address_v6: str | None,
    persistent_keepalive: int,
    status: str,
    notes: str | None,
    metadata: dict | None,
) -> WireGuardPeerUpdate:
    """Build peer update payload."""
    return WireGuardPeerUpdate(
        name=name,
        description=description or None,
        peer_address=peer_address or None,
        peer_address_v6=peer_address_v6,
        persistent_keepalive=persistent_keepalive,
        status=WireGuardPeerStatus(status),
        notes=notes or None,
        metadata_=metadata,
    )


def sync_lan_subnets_and_deploy(
    db: Session,
    *,
    peer: WireGuardPeer,
    server: WireGuardServer,
    previous_lan_subnets: list[str] | None = None,
) -> None:
    """Sync LAN subnet routes and redeploy WireGuard server."""
    from app.services.vpn_routing import sync_lan_subnets
    from app.services.wireguard_system import WireGuardSystemService

    sync_lan_subnets(peer, server, previous_lan_subnets or [])
    db.commit()
    WireGuardSystemService.deploy_server(db, server.id)


def create_peer(db: Session, payload: WireGuardPeerCreate) -> WireGuardPeer:
    """Create peer."""
    return cast(WireGuardPeer, wg_service.wg_peers.create(db, payload))


def update_peer(db: Session, peer_id: UUID, payload: WireGuardPeerUpdate) -> WireGuardPeer:
    """Update peer."""
    return wg_service.wg_peers.update(db, peer_id, payload)


def parse_known_subnets(route_text: str | None) -> tuple[list[str], list[str]]:
    """Parse and validate known-subnet CIDR strings.

    Returns:
        Tuple of (normalized_subnets, errors).
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
            errors.append(f"Invalid known subnet CIDR: {cidr}")
    return normalized, errors


def parse_lan_subnets(route_text: str | None) -> tuple[list[str], list[str]]:
    """Parse and validate LAN subnet CIDR strings, blocking reserved ranges.

    Returns:
        Tuple of (normalized_subnets, errors).
    """
    if not route_text:
        return [], []

    raw_items = [item.strip() for item in route_text.replace("\n", ",").split(",")]
    routes = [item for item in raw_items if item]
    errors: list[str] = []
    normalized: list[str] = []
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


def handle_create_peer(
    db: Session,
    server_id: UUID,
    *,
    name: str,
    description: str | None,
    peer_address: str | None,
    peer_address_v6: str | None,
    persistent_keepalive: int,
    use_preshared_key: bool,
    known_subnets: str | None,
    lan_subnets: str | None,
    notes: str | None,
    actor_id: str | None,
    request: object,
) -> tuple[WireGuardPeer | None, list[str]]:
    """Validate, create a peer, sync LAN subnets, and audit-log.

    Returns:
        Tuple of (created_peer_or_none, errors).
    """
    errors: list[str] = []
    server = wg_service.wg_servers.get(db, server_id)

    known_subnets_list, subnet_errors = parse_known_subnets(known_subnets)
    errors.extend(subnet_errors)

    lan_subnets_list, lan_subnet_errors = parse_lan_subnets(lan_subnets)
    errors.extend(lan_subnet_errors)

    peer_address_v6 = normalize_peer_address_v6(peer_address_v6)
    if errors:
        return None, errors

    try:
        metadata = build_metadata(
            existing_metadata=None,
            known_subnets_list=known_subnets_list,
            lan_subnets_list=lan_subnets_list,
        )
        payload = create_payload(
            server_id=server_id,
            name=name,
            description=description,
            peer_address=peer_address,
            peer_address_v6=peer_address_v6,
            persistent_keepalive=persistent_keepalive,
            use_preshared_key=use_preshared_key,
            notes=notes,
            metadata=metadata,
        )
        created = create_peer(db, payload)

        if lan_subnets_list:
            peer = wg_service.wg_peers.get(db, created.id)
            sync_lan_subnets_and_deploy(db, peer=peer, server=server)

        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="wireguard_peer",
            entity_id=str(created.id),
            actor_id=actor_id,
            metadata={"name": created.name},
        )

        return created, []

    except ValidationError as e:
        return None, [err["msg"] for err in e.errors()]
    except Exception as e:
        return None, [str(e)]


def handle_update_peer(
    db: Session,
    peer_id: UUID,
    *,
    name: str,
    description: str | None,
    peer_address: str | None,
    peer_address_v6: str | None,
    persistent_keepalive: int,
    status: str,
    known_subnets: str | None,
    lan_subnets: str | None,
    notes: str | None,
    actor_id: str | None,
    request: object,
) -> tuple[WireGuardPeer | None, WireGuardPeer, WireGuardServer, list[str]]:
    """Validate, update a peer, sync LAN subnets, and audit-log.

    Returns:
        Tuple of (updated_peer_or_none, original_peer, server, errors).
    """
    errors: list[str] = []
    peer = wg_service.wg_peers.get(db, peer_id)
    server = wg_service.wg_servers.get(db, peer.server_id)
    before_snapshot = model_to_dict(peer, exclude=PEER_AUDIT_EXCLUDE_FIELDS)

    known_subnets_list, subnet_errors = parse_known_subnets(known_subnets)
    errors.extend(subnet_errors)

    lan_subnets_list, lan_subnet_errors = parse_lan_subnets(lan_subnets)
    errors.extend(lan_subnet_errors)

    peer_address_v6 = normalize_peer_address_v6(peer_address_v6)
    if errors:
        return None, peer, server, errors

    try:
        previous_lan_subnets: list[str] = []
        if peer.metadata_:
            previous_lan_subnets = peer.metadata_.get("lan_subnets") or []
        metadata = build_metadata(
            existing_metadata=peer.metadata_,
            known_subnets_list=known_subnets_list,
            lan_subnets_list=lan_subnets_list,
        )

        payload = update_payload(
            name=name,
            description=description,
            peer_address=peer_address,
            peer_address_v6=peer_address_v6,
            persistent_keepalive=persistent_keepalive,
            status=status,
            notes=notes,
            metadata=metadata,
        )
        updated_peer = update_peer(db, peer_id, payload)

        sync_lan_subnets_and_deploy(
            db,
            peer=updated_peer,
            server=server,
            previous_lan_subnets=previous_lan_subnets,
        )

        after_snapshot = model_to_dict(updated_peer, exclude=PEER_AUDIT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        audit_metadata = {"changes": changes} if changes else None
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="wireguard_peer",
            entity_id=str(updated_peer.id),
            actor_id=actor_id,
            metadata=audit_metadata,
        )
        return updated_peer, peer, server, []

    except ValidationError as e:
        return None, peer, server, [err["msg"] for err in e.errors()]
    except Exception as e:
        return None, peer, server, [str(e)]


def get_peer_detail_context(
    db: Session,
    peer_id: UUID,
    *,
    show_keys: bool = False,
) -> dict[str, object]:
    """Build context dict for the peer detail page.

    Returns dict with keys: server, peer_read, peer_model, is_connected,
    connection_logs, mikrotik_script, peer_config, show_keys, activities.
    """
    from app.services.web_vpn_servers import sync_peer_stats_from_interface

    peer = wg_service.wg_peers.get(db, peer_id)
    server = wg_service.wg_servers.get(db, peer.server_id)

    interface_status = None
    if server.public_key:
        from app.services.wireguard_system import WireGuardSystemService

        interface_status = WireGuardSystemService.get_interface_status(
            server.interface_name,
        )
        sync_peer_stats_from_interface(db, server, interface_status)
        peer = wg_service.wg_peers.get(db, peer_id)

    peer_read = wg_service.wg_peers.to_read_schema(peer, db)
    logs = wg_service.wg_connection_logs.list_by_peer(db, peer_id, limit=20)

    now = datetime.now(UTC)
    is_connected = False
    if peer.last_handshake_at:
        is_connected = (now - peer.last_handshake_at).total_seconds() < 180

    mikrotik_script = None
    peer_config = None
    if peer.private_key:
        try:
            mikrotik_script = wg_service.wg_mikrotik.generate_script(db, peer_id)
            peer_config = wg_service.wg_peers.generate_peer_config(db, peer_id)
        except Exception:
            pass

    activities = build_audit_activities(db, "wireguard_peer", str(peer.id), limit=10)

    return {
        "server": server,
        "peer": peer_read,
        "peer_model": peer,
        "is_connected": is_connected,
        "connection_logs": logs,
        "mikrotik_script": mikrotik_script,
        "peer_config": peer_config,
        "show_keys": show_keys,
        "activities": activities,
    }


def disable_peer(
    db: Session,
    peer_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> WireGuardPeer:
    """Disable a peer and audit-log."""
    peer = wg_service.wg_peers.disable(db, peer_id)
    log_audit_event(
        db=db,
        request=request,
        action="disable",
        entity_type="wireguard_peer",
        entity_id=str(peer.id),
        actor_id=actor_id,
        metadata={"status": peer.status.value if peer.status else None},
    )
    return peer


def enable_peer(
    db: Session,
    peer_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> WireGuardPeer:
    """Enable a peer and audit-log."""
    peer = wg_service.wg_peers.enable(db, peer_id)
    log_audit_event(
        db=db,
        request=request,
        action="enable",
        entity_type="wireguard_peer",
        entity_id=str(peer.id),
        actor_id=actor_id,
        metadata={"status": peer.status.value if peer.status else None},
    )
    return peer


def delete_peer(
    db: Session,
    peer_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> UUID:
    """Delete a peer, audit-log, and return the server_id for redirect."""
    peer = wg_service.wg_peers.get(db, peer_id)
    server_id = peer.server_id
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="wireguard_peer",
        entity_id=str(peer.id),
        actor_id=actor_id,
        metadata={"name": peer.name},
    )
    wg_service.wg_peers.delete(db, peer_id)
    return server_id


def regenerate_peer_token(
    db: Session,
    peer_id: UUID,
    *,
    actor_id: str | None,
    request: object,
) -> None:
    """Regenerate provisioning token and audit-log."""
    wg_service.wg_peers.regenerate_provision_token(db, peer_id)
    log_audit_event(
        db=db,
        request=request,
        action="regenerate_token",
        entity_type="wireguard_peer",
        entity_id=str(peer_id),
        actor_id=actor_id,
        metadata=None,
    )
