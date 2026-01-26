"""WireGuard VPN server and peer models.

WireGuard replaces OpenVPN with simpler keypair-based tunnels.
All MikroTik devices run RouterOS 7+ with native WireGuard support.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    BigInteger,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class WireGuardPeerStatus(enum.Enum):
    """Peer connection status."""

    active = "active"
    disabled = "disabled"


class WireGuardServer(Base):
    """WireGuard server/interface configuration.

    Each server represents a WireGuard interface with its own keypair.
    Peers connect to this interface and are assigned IPs from the VPN network.
    """

    __tablename__ = "wireguard_servers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    # WireGuard interface settings
    interface_name: Mapped[str] = mapped_column(String(32), default="wg0")  # Linux interface name
    listen_port: Mapped[int] = mapped_column(Integer, default=51820)

    # Server keypair (private key may be encrypted at rest)
    private_key: Mapped[str | None] = mapped_column(Text)  # Encrypted with Fernet
    public_key: Mapped[str | None] = mapped_column(String(64))  # Base64, 44 chars

    # Public endpoint for peers to connect to
    public_host: Mapped[str | None] = mapped_column(String(255))
    public_port: Mapped[int | None] = mapped_column(Integer)

    # VPN network settings (CIDR format)
    vpn_address: Mapped[str] = mapped_column(
        String(64), default="10.10.0.1/24"
    )  # Server's address in the VPN
    vpn_address_v6: Mapped[str | None] = mapped_column(String(64))
    mtu: Mapped[int] = mapped_column(Integer, default=1420)

    # DNS servers to push to peers (JSON array of IPs)
    dns_servers: Mapped[list | None] = mapped_column(JSON)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Metadata for additional settings
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    peers = relationship(
        "WireGuardPeer", back_populates="server", cascade="all, delete-orphan"
    )


class WireGuardPeer(Base):
    """WireGuard peer configuration.

    Each peer is a VPN client that connects to a WireGuard server.
    Peers are standalone entities - device associations are managed
    through the server's network_device relationship.
    """

    __tablename__ = "wireguard_peers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wireguard_servers.id"), nullable=False
    )

    # Peer identity
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Peer keypair
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)  # Base64
    private_key: Mapped[str | None] = mapped_column(Text)  # Optional, encrypted
    preshared_key: Mapped[str | None] = mapped_column(Text)  # Optional, encrypted

    # IP configuration
    allowed_ips: Mapped[list | None] = mapped_column(
        JSON
    )  # CIDR list, e.g., ["10.10.0.2/32", "192.168.1.0/24"]
    peer_address: Mapped[str | None] = mapped_column(
        String(64)
    )  # Peer's address in VPN
    peer_address_v6: Mapped[str | None] = mapped_column(String(64))

    # WireGuard settings
    persistent_keepalive: Mapped[int] = mapped_column(
        Integer, default=25
    )  # Seconds, 0 to disable

    # Status
    status: Mapped[WireGuardPeerStatus] = mapped_column(
        Enum(WireGuardPeerStatus), default=WireGuardPeerStatus.active
    )

    # Provisioning token (hashed) for device self-registration
    provision_token_hash: Mapped[str | None] = mapped_column(String(128))
    provision_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Connection tracking
    last_handshake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    endpoint_ip: Mapped[str | None] = mapped_column(String(64))  # Real IP of peer
    rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    # Metadata and notes
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    server = relationship("WireGuardServer", back_populates="peers")
    connection_logs = relationship("WireGuardConnectionLog", back_populates="peer")


class WireGuardConnectionLog(Base):
    """Log of WireGuard peer connections.

    Optional auditing table for tracking connection history.
    Entries can be cleaned up via Celery task with retention policy.
    """

    __tablename__ = "wireguard_connection_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    peer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wireguard_peers.id"), nullable=False
    )

    # Connection details
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    endpoint_ip: Mapped[str | None] = mapped_column(String(64))
    peer_address: Mapped[str | None] = mapped_column(String(64))

    # Traffic stats for this session
    rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    # Disconnection info
    disconnect_reason: Mapped[str | None] = mapped_column(String(255))

    # Relationships
    peer = relationship("WireGuardPeer", back_populates="connection_logs")
