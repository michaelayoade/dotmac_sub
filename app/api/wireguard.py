"""WireGuard VPN REST API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_db
from app.models.wireguard import WireGuardPeerStatus
from app.schemas.wireguard import (
    GenerateProvisionTokenRequest,
    MikroTikScriptResponse,
    ProvisionTokenResponse,
    ProvisionWithTokenRequest,
    WireGuardConnectionLogRead,
    WireGuardPeerConfig,
    WireGuardPeerCreate,
    WireGuardPeerCreated,
    WireGuardPeerRead,
    WireGuardPeerUpdate,
    WireGuardServerCreate,
    WireGuardServerRead,
    WireGuardServerStatus,
    WireGuardServerUpdate,
)
from app.services import wireguard as wg_service

router = APIRouter(prefix="/wireguard", tags=["wireguard"])
public_router = APIRouter(prefix="/wireguard-provision", tags=["wireguard-provisioning-public"])


def _assert_peer_download_access(peer: object, current_user: dict) -> None:
    roles = {str(role).strip().lower() for role in (current_user.get("roles") or [])}
    role = current_user.get("role")
    if isinstance(role, str) and role.strip():
        roles.add(role.strip().lower())

    if "admin" in roles or "operator" in roles:
        return

    peer_subscriber_id = getattr(peer, "subscriber_id", None)
    user_ids = {
        str(value)
        for value in (current_user.get("subscriber_id"), current_user.get("principal_id"))
        if value is not None
    }
    if peer_subscriber_id is not None and str(peer_subscriber_id) in user_ids:
        return

    raise HTTPException(status_code=403, detail="Access denied")


# ============== Server Endpoints ==============


@router.get(
    "/servers",
    response_model=list[WireGuardServerRead],
    tags=["wireguard-servers"],
)
def list_servers(
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List all WireGuard servers."""
    servers = wg_service.wg_servers.list(db, is_active=is_active, limit=limit, offset=offset)
    return [wg_service.wg_servers.to_read_schema(s, db) for s in servers]


@router.get(
    "/servers/{server_id}",
    response_model=WireGuardServerRead,
    tags=["wireguard-servers"],
)
def get_server(server_id: UUID, db: Session = Depends(get_db)):
    """Get a WireGuard server by ID."""
    server = wg_service.wg_servers.get(db, server_id)
    return wg_service.wg_servers.to_read_schema(server, db)


@router.post(
    "/servers",
    response_model=WireGuardServerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["wireguard-servers"],
)
def create_server(payload: WireGuardServerCreate, db: Session = Depends(get_db)):
    """Create a new WireGuard server with auto-generated keypair."""
    server = wg_service.wg_servers.create(db, payload)
    return wg_service.wg_servers.to_read_schema(server, db)


@router.patch(
    "/servers/{server_id}",
    response_model=WireGuardServerRead,
    tags=["wireguard-servers"],
)
def update_server(
    server_id: UUID, payload: WireGuardServerUpdate, db: Session = Depends(get_db)
):
    """Update WireGuard server configuration."""
    server = wg_service.wg_servers.update(db, server_id, payload)
    return wg_service.wg_servers.to_read_schema(server, db)


@router.delete(
    "/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["wireguard-servers"],
)
def delete_server(server_id: UUID, db: Session = Depends(get_db)):
    """Delete a WireGuard server and all its peers."""
    wg_service.wg_servers.delete(db, server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/servers/{server_id}/regenerate-keys",
    response_model=WireGuardServerRead,
    tags=["wireguard-servers"],
)
def regenerate_server_keys(server_id: UUID, db: Session = Depends(get_db)):
    """Regenerate server keypair.

    WARNING: This will break all existing peer connections until they
    are reconfigured with the new public key.
    """
    server = wg_service.wg_servers.regenerate_keys(db, server_id)
    return wg_service.wg_servers.to_read_schema(server, db)


@router.get(
    "/servers/{server_id}/status",
    response_model=WireGuardServerStatus,
    tags=["wireguard-servers"],
)
def get_server_status(server_id: UUID, db: Session = Depends(get_db)):
    """Get server status and statistics."""
    status_data = wg_service.wg_servers.get_server_status(db, server_id)
    return WireGuardServerStatus(**status_data)


# ============== Peer Endpoints ==============


@router.get(
    "/peers",
    response_model=list[WireGuardPeerRead],
    tags=["wireguard-peers"],
)
def list_peers(
    server_id: UUID | None = Query(default=None),
    status: WireGuardPeerStatus | None = Query(default=None),
    nas_device_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List WireGuard peers with optional filters."""
    peers = wg_service.wg_peers.list(
        db,
        server_id=server_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [wg_service.wg_peers.to_read_schema(p, db) for p in peers]


@router.get(
    "/peers/{peer_id}",
    response_model=WireGuardPeerRead,
    tags=["wireguard-peers"],
)
def get_peer(peer_id: UUID, db: Session = Depends(get_db)):
    """Get a WireGuard peer by ID."""
    peer = wg_service.wg_peers.get(db, peer_id)
    return wg_service.wg_peers.to_read_schema(peer, db)


@router.post(
    "/peers",
    response_model=WireGuardPeerCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["wireguard-peers"],
)
def create_peer(payload: WireGuardPeerCreate, db: Session = Depends(get_db)):
    """Create a new WireGuard peer with auto-generated keypair.

    The private key and optional preshared key are returned only during creation.
    Store them securely as they cannot be retrieved later.
    """
    return wg_service.wg_peers.create(db, payload)


@router.patch(
    "/peers/{peer_id}",
    response_model=WireGuardPeerRead,
    tags=["wireguard-peers"],
)
def update_peer(
    peer_id: UUID, payload: WireGuardPeerUpdate, db: Session = Depends(get_db)
):
    """Update WireGuard peer configuration."""
    peer = wg_service.wg_peers.update(db, peer_id, payload)
    return wg_service.wg_peers.to_read_schema(peer, db)


@router.delete(
    "/peers/{peer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["wireguard-peers"],
)
def delete_peer(peer_id: UUID, db: Session = Depends(get_db)):
    """Delete a WireGuard peer."""
    wg_service.wg_peers.delete(db, peer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/peers/{peer_id}/disable",
    response_model=WireGuardPeerRead,
    tags=["wireguard-peers"],
)
def disable_peer(peer_id: UUID, db: Session = Depends(get_db)):
    """Disable a WireGuard peer."""
    peer = wg_service.wg_peers.disable(db, peer_id)
    return wg_service.wg_peers.to_read_schema(peer, db)


@router.post(
    "/peers/{peer_id}/enable",
    response_model=WireGuardPeerRead,
    tags=["wireguard-peers"],
)
def enable_peer(peer_id: UUID, db: Session = Depends(get_db)):
    """Enable a WireGuard peer."""
    peer = wg_service.wg_peers.enable(db, peer_id)
    return wg_service.wg_peers.to_read_schema(peer, db)


# ============== Configuration Endpoints ==============


@router.get(
    "/peers/{peer_id}/config",
    response_model=WireGuardPeerConfig,
    tags=["wireguard-config"],
)
def get_peer_config(peer_id: UUID, db: Session = Depends(get_db)):
    """Get WireGuard configuration for a peer.

    Returns a wg-quick compatible configuration file.
    Only available if private key was stored during creation.
    """
    return wg_service.wg_peers.generate_peer_config(db, peer_id)


@router.get(
    "/peers/{peer_id}/config/download",
    response_class=PlainTextResponse,
    tags=["wireguard-config"],
)
def download_peer_config(
    peer_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Download WireGuard configuration file for a peer."""
    peer = wg_service.wg_peers.get(db, peer_id)
    _assert_peer_download_access(peer, current_user)
    config = wg_service.wg_peers.generate_peer_config(db, peer_id)
    return PlainTextResponse(
        content=config.config_content,
        headers={
            "Content-Disposition": f'attachment; filename="{config.filename}"',
            "Content-Type": "text/plain; charset=utf-8",
        },
    )


@router.get(
    "/peers/{peer_id}/mikrotik-script",
    response_model=MikroTikScriptResponse,
    tags=["wireguard-config"],
)
def get_mikrotik_script(peer_id: UUID, db: Session = Depends(get_db)):
    """Get RouterOS 7 script for configuring WireGuard on a MikroTik device."""
    return wg_service.wg_mikrotik.generate_script(db, peer_id)


@router.get(
    "/peers/{peer_id}/mikrotik-script/download",
    response_class=PlainTextResponse,
    tags=["wireguard-config"],
)
def download_mikrotik_script(
    peer_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Download RouterOS script file for a peer."""
    peer = wg_service.wg_peers.get(db, peer_id)
    _assert_peer_download_access(peer, current_user)
    script = wg_service.wg_mikrotik.generate_script(db, peer_id)
    return PlainTextResponse(
        content=script.script_content,
        headers={
            "Content-Disposition": f'attachment; filename="{script.filename}"',
            "Content-Type": "text/plain; charset=utf-8",
        },
    )


# ============== Provisioning Token Endpoints ==============


@router.post(
    "/peers/{peer_id}/provision-token",
    response_model=ProvisionTokenResponse,
    tags=["wireguard-provisioning"],
)
def generate_provision_token(
    peer_id: UUID,
    payload: GenerateProvisionTokenRequest,
    db: Session = Depends(get_db),
):
    """Generate a new provisioning token for a peer.

    Tokens are valid for the specified number of hours (default 24).
    """
    token, expires_at = wg_service.wg_peers.regenerate_provision_token(
        db, peer_id, payload.expires_in_hours
    )

    return ProvisionTokenResponse(
        token=token,
        expires_at=expires_at,
        provision_url=f"/api/v1/wireguard-provision/register?token={token}",
    )


# ============== Connection Log Endpoints ==============


@router.get(
    "/peers/{peer_id}/connection-logs",
    response_model=list[WireGuardConnectionLogRead],
    tags=["wireguard-logs"],
)
def list_peer_connection_logs(
    peer_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List connection logs for a peer."""
    logs = wg_service.wg_connection_logs.list_by_peer_with_names(
        db, peer_id, limit=limit, offset=offset
    )
    return [WireGuardConnectionLogRead(**log) for log in logs]


# ============== Public Provisioning Endpoints ==============
# These do not require authentication - secured by provisioning token


@public_router.post(
    "/register",
    response_model=WireGuardPeerConfig,
    tags=["wireguard-provisioning-public"],
)
def register_with_token(payload: ProvisionWithTokenRequest, db: Session = Depends(get_db)):
    """Self-register a device using a provisioning token.

    The device provides its public key and receives configuration.
    This endpoint does not require authentication.
    """
    return wg_service.wg_peers.register_with_token(db, payload.token, payload.public_key)
