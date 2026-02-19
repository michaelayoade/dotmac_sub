"""WireGuard VPN server and peer management service.

Provides CRUD operations for WireGuard servers and peers,
configuration generation, and MikroTik RouterOS 7 script generation.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import cast

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.wireguard import (
    WireGuardConnectionLog,
    WireGuardPeer,
    WireGuardPeerStatus,
    WireGuardServer,
)
from app.schemas.wireguard import (
    MikroTikScriptResponse,
    WireGuardPeerConfig,
    WireGuardPeerCreate,
    WireGuardPeerCreated,
    WireGuardPeerRead,
    WireGuardPeerUpdate,
    WireGuardServerCreate,
    WireGuardServerRead,
    WireGuardServerUpdate,
)
from app.services.wireguard_crypto import (
    decrypt_private_key,
    encrypt_private_key,
    generate_keypair,
    generate_preshared_key,
    generate_provision_token,
    hash_token,
    validate_key,
)
from app.models.domain_settings import DomainSetting, SettingDomain


def _ensure_utc_aware(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    Some DB backends (notably SQLite) can round-trip tz-aware datetimes back as
    naive, even when columns are declared with timezone=True.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sanitize_interface_name(name: str, max_len: int = 15) -> str:
    """Create a RouterOS-safe interface name.

    WireGuard interfaces have a 15-char limit on some systems.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if not slug:
        slug = "wg"
    return f"wg-{slug[:max_len - 3]}"


def _get_default_vpn_address(db: Session) -> str:
    setting = cast(
        DomainSetting | None,
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.network)
        .filter(DomainSetting.key == "wireguard_default_vpn_address")
        .filter(DomainSetting.is_active.is_(True))
        .first(),
    )
    if setting and setting.value_text:
        value = cast(str, setting.value_text).strip()
        if value and value.lower() != "none":
            return value
    return "10.10.0.1/24"


def _parse_vpn_network(vpn_address: str | None) -> tuple[str, str, int]:
    """Parse VPN address into network components.

    Args:
        vpn_address: CIDR notation like "10.10.0.1/24"

    Returns:
        Tuple of (server_ip, network_address, prefix_length)
    """
    if not vpn_address or str(vpn_address).strip().lower() == "none":
        vpn_address = "10.10.0.1/24"
    try:
        interface = ipaddress.ip_interface(vpn_address)
        return (
            str(interface.ip),
            str(interface.network.network_address),
            interface.network.prefixlen,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid VPN address '{vpn_address}': {e}",
        ) from e


def _normalize_allowed_ips(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    normalized: list[str] = []
    seen = set()
    for value in values:
        entry = (value or "").strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=False)
            else:
                ip = ipaddress.ip_address(entry)
                suffix = "128" if ip.version == 6 else "32"
                network = ipaddress.ip_network(f"{entry}/{suffix}", strict=False)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid allowed IP: {entry}",
            ) from exc
        normalized_value = str(network)
        if normalized_value not in seen:
            normalized.append(normalized_value)
            seen.add(normalized_value)
    return normalized or None


def _allocate_peer_address(
    db: Session,
    server: WireGuardServer,
    vpn_address: str,
    requested_address: str | None = None,
    address_field: str = "peer_address",
) -> str:
    """Allocate an IP address for a new peer.

    Args:
        db: Database session
        server: WireGuard server
        requested_address: Optional specific address to use

    Returns:
        Allocated IP address in CIDR notation (e.g., "10.10.0.2/32")
    """
    server_ip, network_addr, prefix_len = _parse_vpn_network(vpn_address)
    network = ipaddress.ip_network(f"{network_addr}/{prefix_len}", strict=False)
    host_prefix = 128 if network.version == 6 else 32

    # Get all allocated addresses
    existing_peers = (
        db.query(getattr(WireGuardPeer, address_field))
        .filter(WireGuardPeer.server_id == server.id)
        .filter(getattr(WireGuardPeer, address_field).isnot(None))
        .all()
    )
    allocated = set()
    for (addr,) in existing_peers:
        if addr:
            try:
                # Extract IP from CIDR notation
                ip = str(ipaddress.ip_interface(addr).ip)
                allocated.add(ip)
            except ValueError:
                pass

    # Add server IP to allocated set
    allocated.add(server_ip)

    if requested_address:
        # Validate and use requested address
        try:
            req_interface = ipaddress.ip_interface(requested_address)
            req_ip = str(req_interface.ip)
            if req_interface.ip not in network:
                raise HTTPException(
                    status_code=400,
                    detail=f"Address {req_ip} is not in server network {network}",
                )
            if req_ip in allocated:
                raise HTTPException(
                    status_code=400,
                    detail=f"Address {req_ip} is already allocated",
                )
            return f"{req_ip}/{host_prefix}"
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid peer address: {e}",
            ) from e

    # Auto-allocate next available address
    for host in network.hosts():
        host_str = str(host)
        if host_str not in allocated:
            return f"{host_str}/{host_prefix}"

    raise HTTPException(
        status_code=400,
        detail=f"No available addresses in network {network}",
    )


class WireGuardServerService:
    """Service for WireGuard server management."""

    @staticmethod
    def create(db: Session, payload: WireGuardServerCreate) -> WireGuardServer:
        """Create a new WireGuard server with auto-generated keys."""
        # Check for duplicate name
        existing = (
            db.query(WireGuardServer)
            .filter(WireGuardServer.name == payload.name)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400, detail="Server with this name already exists"
            )

        # Generate keypair if not provided
        private_key = payload.private_key
        public_key = payload.public_key

        if not private_key:
            private_key, public_key = generate_keypair()
        elif not public_key:
            # Derive public key from private key
            from app.services.wireguard_crypto import derive_public_key

            public_key = derive_public_key(private_key)

        # Validate keys
        if not validate_key(private_key):
            raise HTTPException(status_code=400, detail="Invalid private key format")
        if not validate_key(public_key):
            raise HTTPException(status_code=400, detail="Invalid public key format")

        # Encrypt private key for storage
        encrypted_private_key = encrypt_private_key(private_key)

        vpn_address = payload.vpn_address or _get_default_vpn_address(db)
        server = WireGuardServer(
            name=payload.name,
            description=payload.description,
            listen_port=payload.listen_port,
            private_key=encrypted_private_key,
            public_key=public_key,
            public_host=payload.public_host,
            public_port=payload.public_port,
            vpn_address=vpn_address,
            vpn_address_v6=payload.vpn_address_v6,
            mtu=payload.mtu,
            dns_servers=payload.dns_servers,
            is_active=payload.is_active,
            metadata_=payload.metadata_,
        )
        db.add(server)
        db.commit()
        db.refresh(server)

        return server

    @staticmethod
    def get(db: Session, server_id: str | uuid.UUID) -> WireGuardServer:
        """Get a server by ID."""
        server = cast(
            WireGuardServer | None,
            db.query(WireGuardServer).filter(WireGuardServer.id == server_id).first(),
        )
        if not server:
            raise HTTPException(status_code=404, detail="WireGuard server not found")
        return server

    @staticmethod
    def get_by_name(db: Session, name: str) -> WireGuardServer | None:
        """Get a server by name."""
        return cast(
            WireGuardServer | None,
            db.query(WireGuardServer).filter(WireGuardServer.name == name).first(),
        )

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WireGuardServer]:
        """List all servers with optional filters."""
        query = db.query(WireGuardServer)
        if is_active is not None:
            query = query.filter(WireGuardServer.is_active == is_active)
        return cast(
            list[WireGuardServer],
            query.order_by(WireGuardServer.name).offset(offset).limit(limit).all(),
        )

    @staticmethod
    def update(
        db: Session, server_id: str | uuid.UUID, payload: WireGuardServerUpdate
    ) -> WireGuardServer:
        """Update server configuration."""
        server = WireGuardServerService.get(db, server_id)
        update_data = payload.model_dump(exclude_unset=True)
        if "vpn_address" in update_data and not update_data["vpn_address"]:
            update_data.pop("vpn_address")

        for key, value in update_data.items():
            setattr(server, key, value)

        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: str | uuid.UUID) -> None:
        """Delete a server and all its peers."""
        server = WireGuardServerService.get(db, server_id)
        db.delete(server)
        db.commit()

    @staticmethod
    def regenerate_keys(db: Session, server_id: str | uuid.UUID) -> WireGuardServer:
        """Regenerate server keypair.

        WARNING: This will break all existing peer connections until they
        are reconfigured with the new public key.
        """
        server = WireGuardServerService.get(db, server_id)

        private_key, public_key = generate_keypair()
        server.private_key = encrypt_private_key(private_key)
        server.public_key = public_key

        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get_peer_count(db: Session, server_id: uuid.UUID) -> int:
        """Get count of peers for a server."""
        return (
            db.query(func.count(WireGuardPeer.id))
            .filter(WireGuardPeer.server_id == server_id)
            .scalar()
            or 0
        )

    @staticmethod
    def to_read_schema(server: WireGuardServer, db: Session | None = None) -> WireGuardServerRead:
        """Convert model to read schema."""
        peer_count = 0
        if db:
            peer_count = WireGuardServerService.get_peer_count(db, server.id)

        return WireGuardServerRead(
            id=server.id,
            name=server.name,
            description=server.description,
            listen_port=server.listen_port,
            public_key=server.public_key,
            public_host=server.public_host,
            public_port=server.public_port,
            vpn_address=server.vpn_address,
            vpn_address_v6=server.vpn_address_v6,
            mtu=server.mtu,
            dns_servers=server.dns_servers,
            is_active=server.is_active,
            metadata_=server.metadata_,
            has_private_key=server.private_key is not None,
            peer_count=peer_count,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )

    @staticmethod
    def get_server_status(db: Session, server_id: str | uuid.UUID) -> dict:
        """Get server status and statistics.

        Returns:
            Dictionary with server status information including:
            - server_id, server_name, is_active
            - total_peers, active_peers, connected_peers
            - total_rx_bytes, total_tx_bytes
        """
        server = WireGuardServerService.get(db, server_id)
        peers = WireGuardPeerService.list(db, server_id=server_id, limit=1000)

        total_rx = sum(p.rx_bytes or 0 for p in peers)
        total_tx = sum(p.tx_bytes or 0 for p in peers)
        active_peers = sum(1 for p in peers if p.status == WireGuardPeerStatus.active)

        # Count peers with recent handshake (within 3 minutes)
        now = datetime.now(timezone.utc)
        connected_peers = sum(
            1
            for p in peers
            if p.last_handshake_at and (now - p.last_handshake_at).total_seconds() < 180
        )

        return {
            "server_id": server.id,
            "server_name": server.name,
            "is_active": server.is_active,
            "total_peers": len(peers),
            "active_peers": active_peers,
            "connected_peers": connected_peers,
            "total_rx_bytes": total_rx,
            "total_tx_bytes": total_tx,
        }


class WireGuardPeerService:
    """Service for WireGuard peer management."""

    @staticmethod
    def create(db: Session, payload: WireGuardPeerCreate) -> WireGuardPeerCreated:
        """Create a new peer with auto-generated keys.

        Returns the peer with private key (only time it's shown).
        """
        server = WireGuardServerService.get(db, payload.server_id)
        if not server.vpn_address:
            server.vpn_address = _get_default_vpn_address(db)
            db.commit()
            db.refresh(server)

        # Generate keypair if not provided
        private_key = payload.private_key
        public_key = payload.public_key

        if not private_key:
            private_key, public_key = generate_keypair()
        elif not public_key:
            from app.services.wireguard_crypto import derive_public_key

            public_key = derive_public_key(private_key)

        # Validate keys
        if not validate_key(public_key):
            raise HTTPException(status_code=400, detail="Invalid public key format")
        if private_key and not validate_key(private_key):
            raise HTTPException(status_code=400, detail="Invalid private key format")

        # Check for duplicate public key on this server
        existing = (
            db.query(WireGuardPeer)
            .filter(
                WireGuardPeer.server_id == server.id,
                WireGuardPeer.public_key == public_key,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Peer with this public key already exists on this server",
            )

        # Allocate peer addresses (IPv4 + optional IPv6)
        peer_address = _allocate_peer_address(
            db, server, server.vpn_address, payload.peer_address, "peer_address"
        )
        peer_address_v6 = None
        if payload.peer_address_v6 and not server.vpn_address_v6:
            raise HTTPException(
                status_code=400,
                detail="IPv6 address provided but server has no IPv6 VPN address.",
            )
        if server.vpn_address_v6:
            peer_address_v6 = _allocate_peer_address(
                db, server, server.vpn_address_v6, payload.peer_address_v6, "peer_address_v6"
            )

        # Set allowed_ips to peer address if not provided
        allowed_ips = _normalize_allowed_ips(payload.allowed_ips)
        if not allowed_ips:
            allowed_ips = [peer_address]
            if peer_address_v6:
                allowed_ips.append(peer_address_v6)

        # Generate preshared key if requested
        preshared_key = None
        encrypted_psk = None
        if payload.use_preshared_key:
            preshared_key = generate_preshared_key()
            encrypted_psk = encrypt_private_key(preshared_key)

        # Encrypt private key for storage (optional - may not store it)
        encrypted_private_key = None
        if private_key:
            encrypted_private_key = encrypt_private_key(private_key)

        # Generate provisioning token
        provision_token = generate_provision_token()
        token_hash = hash_token(provision_token)
        token_expires = datetime.now(timezone.utc) + timedelta(hours=24)

        peer = WireGuardPeer(
            server_id=server.id,
            name=payload.name,
            description=payload.description,
            public_key=public_key,
            private_key=encrypted_private_key,
            preshared_key=encrypted_psk,
            allowed_ips=allowed_ips,
            peer_address=peer_address,
            peer_address_v6=peer_address_v6,
            persistent_keepalive=payload.persistent_keepalive,
            status=payload.status,
            notes=payload.notes,
            metadata_=payload.metadata_,
            provision_token_hash=token_hash,
            provision_token_expires_at=token_expires,
        )
        db.add(peer)
        db.commit()
        db.refresh(peer)

        # Auto-deploy to local WireGuard interface (non-blocking)
        WireGuardPeerService._auto_deploy(db, server)

        # Return with keys (only time they're shown)
        return WireGuardPeerCreated(
            **WireGuardPeerService.to_read_schema(peer, db).model_dump(),
            private_key=private_key,
            preshared_key=preshared_key,
            provision_token=provision_token,
        )

    @staticmethod
    def get(db: Session, peer_id: str | uuid.UUID) -> WireGuardPeer:
        """Get a peer by ID."""
        peer = cast(
            WireGuardPeer | None,
            db.query(WireGuardPeer).filter(WireGuardPeer.id == peer_id).first(),
        )
        if not peer:
            raise HTTPException(status_code=404, detail="WireGuard peer not found")
        return peer

    @staticmethod
    def list(
        db: Session,
        server_id: str | uuid.UUID | None = None,
        status: WireGuardPeerStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WireGuardPeer]:
        """List peers with eager loading to avoid N+1 queries."""
        query = db.query(WireGuardPeer).options(
            joinedload(WireGuardPeer.server),
        )

        if server_id:
            query = query.filter(WireGuardPeer.server_id == server_id)
        if status:
            query = query.filter(WireGuardPeer.status == status)

        return cast(
            list[WireGuardPeer],
            query.order_by(WireGuardPeer.name).offset(offset).limit(limit).all(),
        )

    @staticmethod
    def update(
        db: Session, peer_id: str | uuid.UUID, payload: WireGuardPeerUpdate
    ) -> WireGuardPeer:
        """Update peer configuration."""
        peer = WireGuardPeerService.get(db, peer_id)
        server = WireGuardServerService.get(db, peer.server_id)
        update_data = payload.model_dump(exclude_unset=True)
        if "metadata_" in update_data and isinstance(update_data["metadata_"], dict):
            update_data["metadata_"] = dict(update_data["metadata_"])

        if update_data.get("peer_address_v6") and not server.vpn_address_v6:
            raise HTTPException(
                status_code=400,
                detail="IPv6 address provided but server has no IPv6 VPN address.",
            )
        if "allowed_ips" in update_data:
            update_data["allowed_ips"] = _normalize_allowed_ips(update_data["allowed_ips"])

        for key, value in update_data.items():
            setattr(peer, key, value)

        if "metadata_" in update_data:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(peer, "metadata_")

        db.commit()
        db.refresh(peer)

        # Auto-deploy if peer config changed
        WireGuardPeerService._auto_deploy(db, server)

        return peer

    @staticmethod
    def _auto_deploy(db: Session, server: WireGuardServer) -> None:
        """Auto-deploy WireGuard config if enabled for this server."""
        import logging
        logger = logging.getLogger(__name__)

        # Check if auto_deploy is enabled
        if not server.metadata_ or not server.metadata_.get("auto_deploy", True):
            return

        if not server.public_key:
            logger.warning("Cannot auto-deploy: server has no keys")
            return

        try:
            from app.services.wireguard_system import WireGuardSystemService
            success, msg = WireGuardSystemService.deploy_server(db, server.id)
            if not success:
                logger.warning(f"Auto-deploy failed: {msg}")
            else:
                logger.info(f"Auto-deployed WireGuard config for {server.name}")
        except Exception as e:
            logger.warning(f"Auto-deploy error: {e}")

    @staticmethod
    def delete(db: Session, peer_id: str | uuid.UUID) -> None:
        """Delete a peer."""
        peer = WireGuardPeerService.get(db, peer_id)
        server = WireGuardServerService.get(db, peer.server_id)

        db.delete(peer)
        db.commit()

        # Auto-deploy to update config (non-blocking)
        WireGuardPeerService._auto_deploy(db, server)

    @staticmethod
    def disable(db: Session, peer_id: str | uuid.UUID) -> WireGuardPeer:
        """Disable a peer (sets status to disabled)."""
        peer = WireGuardPeerService.get(db, peer_id)
        server = WireGuardServerService.get(db, peer.server_id)
        peer.status = WireGuardPeerStatus.disabled
        db.commit()
        db.refresh(peer)

        # Auto-deploy to remove peer from config
        WireGuardPeerService._auto_deploy(db, server)

        return peer

    @staticmethod
    def enable(db: Session, peer_id: str | uuid.UUID) -> WireGuardPeer:
        """Enable a peer (sets status to active)."""
        peer = WireGuardPeerService.get(db, peer_id)
        server = WireGuardServerService.get(db, peer.server_id)
        peer.status = WireGuardPeerStatus.active
        db.commit()
        db.refresh(peer)

        # Auto-deploy to add peer back to config
        WireGuardPeerService._auto_deploy(db, server)

        return peer

    @staticmethod
    def regenerate_provision_token(
        db: Session, peer_id: str | uuid.UUID, expires_in_hours: int = 24
    ) -> tuple[str, datetime]:
        """Regenerate provisioning token for a peer.

        Returns:
            Tuple of (token, expires_at)
        """
        peer = WireGuardPeerService.get(db, peer_id)

        token = generate_provision_token()
        peer.provision_token_hash = hash_token(token)
        peer.provision_token_expires_at = datetime.now(timezone.utc) + timedelta(
            hours=expires_in_hours
        )

        db.commit()
        db.refresh(peer)

        # Normalise for safe comparisons in callers/tests.
        if peer.provision_token_expires_at is None:
            raise HTTPException(status_code=500, detail="Provision token expiry missing")
        return token, _ensure_utc_aware(peer.provision_token_expires_at)

    @staticmethod
    def verify_provision_token(db: Session, token: str) -> WireGuardPeer | None:
        """Verify a provisioning token and return the peer if valid."""
        token_hash = hash_token(token)

        peer = cast(
            WireGuardPeer | None,
            db.query(WireGuardPeer)
            .filter(WireGuardPeer.provision_token_hash == token_hash)
            .first(),
        )

        if not peer:
            return None

        if peer.provision_token_expires_at:
            expires_at = _ensure_utc_aware(peer.provision_token_expires_at)
            if expires_at < datetime.now(timezone.utc):
                return None  # Token expired

        return peer

    @staticmethod
    def register_with_token(
        db: Session, token: str, public_key: str
    ) -> WireGuardPeerConfig:
        """Register a device using a provisioning token.

        The device provides its public key and receives configuration.

        Args:
            db: Database session
            token: Provisioning token
            public_key: Device-generated public key

        Returns:
            WireGuardPeerConfig with configuration for the peer

        Raises:
            HTTPException: If token is invalid/expired or public key is invalid
        """
        # Verify token
        peer = WireGuardPeerService.verify_provision_token(db, token)
        if not peer:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired provisioning token",
            )

        # Validate public key format
        if not validate_key(public_key):
            raise HTTPException(
                status_code=400,
                detail="Invalid public key format",
            )

        # Update peer with new public key (device-generated)
        peer.public_key = public_key
        peer.private_key = None  # Clear any stored private key since device has its own

        # Invalidate token after use
        peer.provision_token_hash = None
        peer.provision_token_expires_at = None

        db.commit()
        db.refresh(peer)

        # Generate and return configuration
        server = WireGuardServerService.get(db, peer.server_id)

        # Build minimal config with just the Peer section
        _, network_addr, prefix_len = _parse_vpn_network(server.vpn_address)
        allowed_networks = [f"{network_addr}/{prefix_len}"]
        vpn_address_v6 = server.vpn_address_v6
        if vpn_address_v6 and str(vpn_address_v6).strip().lower() != "none":
            _, network_addr_v6, prefix_len_v6 = _parse_vpn_network(vpn_address_v6)
            allowed_networks.append(f"{network_addr_v6}/{prefix_len_v6}")
        host = server.public_host or "YOUR_SERVER_IP"
        port = server.public_port or server.listen_port

        lines = [
            "# WireGuard Peer Configuration",
            f"# Add this [Peer] section to your WireGuard interface",
            "",
            "[Peer]",
            f"PublicKey = {server.public_key}",
            f"Endpoint = {host}:{port}",
            f"AllowedIPs = {', '.join(allowed_networks)}",
        ]
        if peer.persistent_keepalive and peer.persistent_keepalive > 0:
            lines.append(f"PersistentKeepalive = {peer.persistent_keepalive}")

        lines.extend([
            "",
            "# Your interface settings:",
            f"# Address = {peer.peer_address}",
        ])
        if peer.peer_address_v6:
            lines.append(f"# Address (IPv6) = {peer.peer_address_v6}")
        if server.mtu:
            lines.append(f"# MTU = {server.mtu}")
        if server.dns_servers:
            lines.append(f"# DNS = {', '.join(server.dns_servers)}")

        config_content = "\n".join(lines) + "\n"

        return WireGuardPeerConfig(
            peer_name=peer.name,
            server_name=server.name,
            config_content=config_content,
            filename=f"wg-{peer.name}-peer.conf",
        )

    @staticmethod
    def generate_peer_config(db: Session, peer_id: str | uuid.UUID) -> WireGuardPeerConfig:
        """Generate WireGuard configuration file for a peer.

        Returns:
            Config object with wg-quick compatible configuration
        """
        peer = WireGuardPeerService.get(db, peer_id)
        server = WireGuardServerService.get(db, peer.server_id)

        if not peer.private_key:
            raise HTTPException(
                status_code=400,
                detail="Peer private key not stored. Generate config during peer creation.",
            )

        # Decrypt private key
        try:
            private_key = decrypt_private_key(peer.private_key)
        except ValueError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to decrypt private key: {e}",
            ) from e

        # Build [Interface] section
        lines = [
            "# WireGuard Configuration",
            f"# Peer: {peer.name}",
            f"# Server: {server.name}",
            f"# Generated: {datetime.now(timezone.utc).isoformat()}",
            "",
            "[Interface]",
            f"PrivateKey = {private_key}",
            f"Address = {', '.join([addr for addr in [peer.peer_address, peer.peer_address_v6] if addr])}",
        ]

        if server.dns_servers:
            lines.append(f"DNS = {', '.join(server.dns_servers)}")

        if server.mtu:
            lines.append(f"MTU = {server.mtu}")

        # Build [Peer] section (server as peer)
        lines.extend([
            "",
            "[Peer]",
            f"PublicKey = {server.public_key}",
        ])

        # Add preshared key if present
        if peer.preshared_key:
            try:
                psk = decrypt_private_key(peer.preshared_key)
                lines.append(f"PresharedKey = {psk}")
            except ValueError:
                pass  # Skip if decryption fails

        # Server endpoint
        host = server.public_host or "YOUR_SERVER_IP"
        port = server.public_port or server.listen_port
        lines.append(f"Endpoint = {host}:{port}")

        # AllowedIPs for routing
        _, network_addr, prefix_len = _parse_vpn_network(server.vpn_address)
        allowed_networks = [f"{network_addr}/{prefix_len}"]
        vpn_address_v6 = server.vpn_address_v6
        if vpn_address_v6 and str(vpn_address_v6).strip().lower() != "none":
            _, network_addr_v6, prefix_len_v6 = _parse_vpn_network(vpn_address_v6)
            allowed_networks.append(f"{network_addr_v6}/{prefix_len_v6}")
        lines.append(f"AllowedIPs = {', '.join(allowed_networks)}")

        # Persistent keepalive
        if peer.persistent_keepalive and peer.persistent_keepalive > 0:
            lines.append(f"PersistentKeepalive = {peer.persistent_keepalive}")

        config_content = "\n".join(lines) + "\n"
        filename = f"wg-{_sanitize_interface_name(peer.name)}.conf"

        return WireGuardPeerConfig(
            peer_name=peer.name,
            server_name=server.name,
            config_content=config_content,
            filename=filename,
        )

    @staticmethod
    def to_read_schema(peer: WireGuardPeer, db: Session | None = None) -> WireGuardPeerRead:
        """Convert model to read schema."""
        server_name = None

        if db and peer.server_id:
            server = (
                db.query(WireGuardServer)
                .filter(WireGuardServer.id == peer.server_id)
                .first()
            )
            server_name = server.name if server else None

        return WireGuardPeerRead(
            id=peer.id,
            server_id=peer.server_id,
            name=peer.name,
            description=peer.description,
            public_key=peer.public_key,
            has_private_key=peer.private_key is not None,
            has_preshared_key=peer.preshared_key is not None,
            allowed_ips=peer.allowed_ips,
            peer_address=peer.peer_address,
            peer_address_v6=peer.peer_address_v6,
            persistent_keepalive=peer.persistent_keepalive,
            status=peer.status,
            notes=peer.notes,
            metadata_=peer.metadata_,
            last_handshake_at=peer.last_handshake_at,
            endpoint_ip=peer.endpoint_ip,
            rx_bytes=peer.rx_bytes,
            tx_bytes=peer.tx_bytes,
            has_provision_token=peer.provision_token_hash is not None,
            provision_token_expires_at=peer.provision_token_expires_at,
            created_at=peer.created_at,
            updated_at=peer.updated_at,
            server_name=server_name,
        )


class MikroTikScriptService:
    """Service for generating MikroTik RouterOS 7 WireGuard scripts."""

    @staticmethod
    def generate_script(db: Session, peer_id: str | uuid.UUID) -> MikroTikScriptResponse:
        """Generate a RouterOS 7 script to configure WireGuard on a MikroTik device.

        The script:
        1. Creates WireGuard interface with peer's private key
        2. Adds IP address
        3. Configures server as peer
        4. Adds firewall rules
        """
        peer = WireGuardPeerService.get(db, peer_id)
        server = WireGuardServerService.get(db, peer.server_id)

        if not peer.private_key:
            raise HTTPException(
                status_code=400,
                detail="Peer private key not stored. Cannot generate MikroTik script.",
            )

        # Decrypt keys
        try:
            private_key = decrypt_private_key(peer.private_key)
        except ValueError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to decrypt private key: {e}",
            ) from e

        preshared_key = None
        if peer.preshared_key:
            try:
                preshared_key = decrypt_private_key(peer.preshared_key)
            except ValueError:
                pass

        # Interface name (max 15 chars for older RouterOS)
        iface_name = _sanitize_interface_name(peer.name)

        # Parse peer address for IP assignment
        _, network_addr, prefix_len = _parse_vpn_network(server.vpn_address)
        network_addr_v6 = None
        prefix_len_v6 = None
        vpn_address_v6 = server.vpn_address_v6
        if vpn_address_v6 and str(vpn_address_v6).strip().lower() != "none":
            _, network_addr_v6, prefix_len_v6 = _parse_vpn_network(vpn_address_v6)

        # Server endpoint
        host = server.public_host or "YOUR_SERVER_IP"
        port = server.public_port or server.listen_port

        # Build allowed addresses for the server peer (return routing).
        # Include VPN network plus any server routes (e.g., LANs behind other peers).
        allowed_addresses = [f"{network_addr}/{prefix_len}"]
        if network_addr_v6 and prefix_len_v6 is not None:
            allowed_addresses.append(f"{network_addr_v6}/{prefix_len_v6}")
        routes: list[str] = []
        if server.metadata_:
            routes_obj: object = server.metadata_.get("routes")
            if isinstance(routes_obj, list):
                routes = [str(r) for r in routes_obj if r]
        for route in routes:
            if route and route not in allowed_addresses:
                allowed_addresses.append(route)

        # Build RouterOS 7 script
        script_lines = [
            "# WireGuard Configuration for MikroTik RouterOS 7+",
            f"# Peer: {peer.name}",
            f"# Server: {server.name}",
            f"# Generated: {datetime.now(timezone.utc).isoformat()}",
            "#",
            "# Run this script on your MikroTik device via terminal or Winbox",
            "",
            "# Remove existing interface if present",
            f':if ([:len [/interface/wireguard/find name="{iface_name}"]] > 0) do={{',
            f'    /interface/wireguard/remove [find name="{iface_name}"]',
            f'    :log info "Removed existing WireGuard interface {iface_name}"',
            "}",
            "",
            "# Create WireGuard interface",
            f'/interface/wireguard/add name="{iface_name}" \\',
            f'    private-key="{private_key}" \\',
            f"    listen-port=0 \\",
            f"    mtu={server.mtu}",
            "",
            "# Add IP address",
            f":if ([:len [/ip/address/find interface={iface_name}]] = 0) do={{",
            f'    /ip/address/add address="{peer.peer_address}" interface="{iface_name}"',
            "}",
        ]
        if peer.peer_address_v6:
            script_lines.extend([
                "",
                "# Add IPv6 address",
                f":if ([:len [/ipv6/address/find interface={iface_name}]] = 0) do={{",
                f'    /ipv6/address/add address="{peer.peer_address_v6}" interface="{iface_name}"',
                "}",
            ])
        script_lines.extend([
            "",
            "# Add server as peer",
            f":local serverPeer [/interface/wireguard/peers/find interface={iface_name}]",
            ":if ([:len $serverPeer] > 0) do={",
            "    /interface/wireguard/peers/remove $serverPeer",
            "}",
            "",
            f"/interface/wireguard/peers/add \\",
            f'    interface="{iface_name}" \\',
            f'    public-key="{server.public_key}" \\',
            f'    endpoint-address="{host}" \\',
            f"    endpoint-port={port} \\",
            f'    allowed-address="{",".join(allowed_addresses)}" \\',
            f"    persistent-keepalive={peer.persistent_keepalive}s"
            + (f' \\' if preshared_key else ''),
        ])

        if preshared_key:
            script_lines.append(f'    preshared-key="{preshared_key}"')

        script_lines.extend([
            "",
            "# Enable interface",
            f'/interface/wireguard/enable [find name="{iface_name}"]',
            "",
            "# Add static routes via WireGuard interface",
        ])

        # Add route for each allowed address (VPN network + server LAN if configured)
        for idx, addr in enumerate(allowed_addresses):
            if ":" in addr:
                script_lines.extend([
                    f':local route6_{idx} [/ipv6/route/find where dst-address="{addr}"]',
                    f":if ([:len $route6_{idx}] = 0) do={{",
                    f'    /ipv6/route/add dst-address="{addr}" gateway="{iface_name}"',
                    "}",
                ])
            else:
                script_lines.extend([
                    f':local route{idx} [/ip/route/find where dst-address="{addr}"]',
                    f":if ([:len $route{idx}] = 0) do={{",
                    f'    /ip/route/add dst-address="{addr}" gateway="{iface_name}"',
                    "}",
                ])

        script_lines.append("")
        script_lines.append(f':log info "WireGuard interface {iface_name} configured successfully"')
        script_lines.extend([
            "",
            "# Verify configuration",
            f'/interface/wireguard/print where name="{iface_name}"',
            f"/interface/wireguard/peers/print where interface={iface_name}",
        ])

        script_content = "\n".join(script_lines) + "\n"
        filename = f"wg-{iface_name}-mikrotik.rsc"

        return MikroTikScriptResponse(
            peer_name=peer.name,
            server_name=server.name,
            script_content=script_content,
            filename=filename,
        )


class WireGuardConnectionLogService:
    """Service for connection logging."""

    @staticmethod
    def log_connect(
        db: Session,
        peer_id: str | uuid.UUID,
        endpoint_ip: str,
        peer_address: str,
    ) -> WireGuardConnectionLog:
        """Log a connection event."""
        log = WireGuardConnectionLog(
            peer_id=peer_id if isinstance(peer_id, uuid.UUID) else uuid.UUID(peer_id),
            connected_at=datetime.now(timezone.utc),
            endpoint_ip=endpoint_ip,
            peer_address=peer_address,
        )
        db.add(log)

        # Update peer
        peer = db.query(WireGuardPeer).filter(WireGuardPeer.id == peer_id).first()
        if peer:
            peer.last_handshake_at = log.connected_at
            peer.endpoint_ip = endpoint_ip

        db.commit()
        db.refresh(log)
        return log

    @staticmethod
    def log_disconnect(
        db: Session,
        log_id: str | uuid.UUID,
        rx_bytes: int = 0,
        tx_bytes: int = 0,
        reason: str | None = None,
    ) -> WireGuardConnectionLog:
        """Log a disconnection event."""
        log = cast(
            WireGuardConnectionLog | None,
            db.query(WireGuardConnectionLog)
            .filter(WireGuardConnectionLog.id == log_id)
            .first(),
        )
        if not log:
            raise HTTPException(status_code=404, detail="Connection log not found")

        log.disconnected_at = datetime.now(timezone.utc)
        log.rx_bytes = rx_bytes
        log.tx_bytes = tx_bytes
        log.disconnect_reason = reason

        # Update peer traffic stats
        peer = db.query(WireGuardPeer).filter(WireGuardPeer.id == log.peer_id).first()
        if peer:
            peer.rx_bytes += rx_bytes
            peer.tx_bytes += tx_bytes

        db.commit()
        db.refresh(log)
        return log

    @staticmethod
    def list_by_peer(
        db: Session,
        peer_id: str | uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WireGuardConnectionLog]:
        """List connection logs for a peer."""
        return cast(
            list[WireGuardConnectionLog],
            db.query(WireGuardConnectionLog)
            .filter(WireGuardConnectionLog.peer_id == peer_id)
            .order_by(WireGuardConnectionLog.connected_at.desc())
            .offset(offset)
            .limit(limit)
            .all(),
        )

    @staticmethod
    def list_by_peer_with_names(
        db: Session,
        peer_id: str | uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List connection logs for a peer with peer names included.

        Returns:
            List of dictionaries with log data and peer_name field.
        """
        # Verify peer exists and get its name
        peer = WireGuardPeerService.get(db, peer_id)
        peer_name = peer.name

        logs = (
            db.query(WireGuardConnectionLog)
            .filter(WireGuardConnectionLog.peer_id == peer_id)
            .order_by(WireGuardConnectionLog.connected_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return [
            {
                "id": log.id,
                "peer_id": log.peer_id,
                "connected_at": log.connected_at,
                "disconnected_at": log.disconnected_at,
                "endpoint_ip": log.endpoint_ip,
                "peer_address": log.peer_address,
                "rx_bytes": log.rx_bytes,
                "tx_bytes": log.tx_bytes,
                "disconnect_reason": log.disconnect_reason,
                "peer_name": peer_name,
            }
            for log in logs
        ]

    @staticmethod
    def cleanup_old_logs(db: Session, days: int = 90) -> int:
        """Delete logs older than specified days.

        Returns:
            Number of deleted records
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = cast(
            int,
            db.query(WireGuardConnectionLog)
            .filter(WireGuardConnectionLog.connected_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return result


class RouterSyncService:
    """Service for syncing WireGuard peers to the core router."""

    @staticmethod
    def _get_router_connection(server: WireGuardServer) -> dict | None:
        """Get router connection settings from server metadata."""
        if not server.metadata_:
            return None
        router_config = server.metadata_.get("router")
        if not router_config:
            return None

        # Get password: try metadata first (encrypted), then env var
        password = None
        encrypted_password = router_config.get("password")
        if encrypted_password:
            try:
                password = decrypt_private_key(encrypted_password)
            except Exception:
                pass

        if not password:
            import os
            password = os.environ.get("WIREGUARD_ROUTER_API_PASSWORD")

        if not password:
            return None

        return {
            "host": router_config.get("host"),
            "port": router_config.get("api_port", 8728),
            "ssl": router_config.get("api_ssl", False),
            "username": router_config.get("username"),
            "password": password,
            "interface_name": router_config.get("interface_name", "wg-infra"),
        }

    @staticmethod
    def sync_peer_to_router(
        db: Session, peer: WireGuardPeer, server: WireGuardServer | None = None
    ) -> tuple[bool, str]:
        """Add a peer to the core router's WireGuard interface.

        Returns:
            Tuple of (success, message)
        """
        import routeros_api

        if server is None:
            server = WireGuardServerService.get(db, peer.server_id)

        conn = RouterSyncService._get_router_connection(server)
        if not conn:
            return False, "Router connection not configured or password not set"

        try:
            # Connect to router
            pool = routeros_api.RouterOsApiPool(
                host=conn["host"],
                username=conn["username"],
                password=conn["password"],
                port=conn["port"],
                use_ssl=conn["ssl"],
                plaintext_login=True,
                ssl_verify=False,
            )
            api = pool.get_api()

            # Get the WireGuard peers resource
            peers_resource = api.get_resource("/interface/wireguard/peers")

            allowed_addresses = list(peer.allowed_ips or [])
            if not allowed_addresses:
                if peer.peer_address:
                    allowed_addresses.append(peer.peer_address)
                if peer.peer_address_v6:
                    allowed_addresses.append(peer.peer_address_v6)
            allowed_address_value = ",".join(allowed_addresses) if allowed_addresses else ""

            # Check if peer already exists (by public key)
            existing = peers_resource.get(
                interface=conn["interface_name"],
                **{"public-key": peer.public_key}
            )

            if existing:
                # Update existing peer
                peer_id = existing[0]["id"]
                peers_resource.set(
                    id=peer_id,
                    **{
                        "allowed-address": allowed_address_value,
                        "comment": peer.name,
                    }
                )
                pool.disconnect()
                return True, f"Peer updated on router (id={peer_id})"
            else:
                # Add new peer
                peers_resource.add(
                    interface=conn["interface_name"],
                    **{
                        "public-key": peer.public_key,
                        "allowed-address": allowed_address_value,
                        "comment": peer.name,
                    }
                )
                pool.disconnect()
                return True, "Peer added to router"

        except routeros_api.exceptions.RouterOsApiCommunicationError as e:
            return False, f"Router communication error: {e}"
        except Exception as e:
            return False, f"Failed to sync peer to router: {e}"

    @staticmethod
    def remove_peer_from_router(
        db: Session, peer: WireGuardPeer, server: WireGuardServer | None = None
    ) -> tuple[bool, str]:
        """Remove a peer from the core router's WireGuard interface.

        Returns:
            Tuple of (success, message)
        """
        import routeros_api

        if server is None:
            server = WireGuardServerService.get(db, peer.server_id)

        conn = RouterSyncService._get_router_connection(server)
        if not conn:
            return False, "Router connection not configured or password not set"

        try:
            pool = routeros_api.RouterOsApiPool(
                host=conn["host"],
                username=conn["username"],
                password=conn["password"],
                port=conn["port"],
                use_ssl=conn["ssl"],
                plaintext_login=True,
                ssl_verify=False,
            )
            api = pool.get_api()

            peers_resource = api.get_resource("/interface/wireguard/peers")

            # Find peer by public key
            existing = peers_resource.get(
                interface=conn["interface_name"],
                **{"public-key": peer.public_key}
            )

            if existing:
                peer_id = existing[0]["id"]
                peers_resource.remove(id=peer_id)
                pool.disconnect()
                return True, f"Peer removed from router (id={peer_id})"
            else:
                pool.disconnect()
                return True, "Peer not found on router (already removed)"

        except routeros_api.exceptions.RouterOsApiCommunicationError as e:
            return False, f"Router communication error: {e}"
        except Exception as e:
            return False, f"Failed to remove peer from router: {e}"

    @staticmethod
    def sync_all_peers(db: Session, server_id: str | uuid.UUID) -> list[tuple[str, bool, str]]:
        """Sync all peers for a server to the router.

        Returns:
            List of (peer_name, success, message) tuples
        """
        server = WireGuardServerService.get(db, server_id)
        peers = WireGuardPeerService.list(db, server_id=server_id, limit=1000)

        results = []
        for peer in peers:
            if peer.status == WireGuardPeerStatus.active:
                success, msg = RouterSyncService.sync_peer_to_router(db, peer, server)
                results.append((peer.name, success, msg))

        return results


# Service instances for module-level access
wg_servers = WireGuardServerService()
wg_peers = WireGuardPeerService()
wg_mikrotik = MikroTikScriptService()
wg_connection_logs = WireGuardConnectionLogService()
wg_router_sync = RouterSyncService()
