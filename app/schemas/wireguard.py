"""Pydantic schemas for WireGuard server and peer management."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.wireguard import WireGuardPeerStatus


# ============== WireGuard Server Schemas ==============


class WireGuardServerBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    interface_name: str = Field(
        default="wg0",
        min_length=1,
        max_length=32,
        description="Linux interface name (e.g., wg0, wg-infra)",
    )
    listen_port: int = Field(default=51820, ge=1, le=65535)
    public_host: str | None = None
    public_port: int | None = Field(default=None, ge=1, le=65535)
    vpn_address: str = Field(
        default="10.10.0.1/24",
        description="Server's address in CIDR notation (e.g., 10.10.0.1/24)",
    )
    vpn_address_v6: str | None = Field(
        default=None,
        description="Server's IPv6 address in CIDR notation (e.g., fd00::1/64)",
    )
    mtu: int = Field(default=1420, ge=1280, le=9000)
    dns_servers: list[str] | None = None
    is_active: bool = True
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class WireGuardServerCreate(WireGuardServerBase):
    """Create a new WireGuard server.

    If no keys are provided, they will be auto-generated.
    """

    # Keys are optional - auto-generated if not provided
    private_key: str | None = None
    public_key: str | None = None


class WireGuardServerUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    interface_name: str | None = Field(default=None, min_length=1, max_length=32)
    listen_port: int | None = Field(default=None, ge=1, le=65535)
    public_host: str | None = None
    public_port: int | None = Field(default=None, ge=1, le=65535)
    vpn_address: str | None = None
    vpn_address_v6: str | None = None
    mtu: int | None = Field(default=None, ge=1280, le=9000)
    dns_servers: list[str] | None = None
    is_active: bool | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class WireGuardServerRead(WireGuardServerBase):
    """Server response schema (excludes private key for security)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    public_key: str | None = None
    has_private_key: bool = False
    peer_count: int = 0
    created_at: datetime
    updated_at: datetime


class WireGuardServerDetail(WireGuardServerRead):
    """Detailed server info including interface config."""

    interface_config: str | None = None  # [Interface] section for wg-quick


# ============== WireGuard Peer Schemas ==============


class WireGuardPeerBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    allowed_ips: list[str] | None = Field(
        default=None,
        description="List of CIDRs the peer can route (e.g., ['10.10.0.2/32'])",
    )
    peer_address: str | None = Field(
        default=None,
        description="Peer's address in the VPN (auto-allocated if not provided)",
    )
    peer_address_v6: str | None = Field(
        default=None,
        description="Peer's IPv6 address in the VPN (auto-allocated if not provided)",
    )
    persistent_keepalive: int = Field(
        default=25,
        ge=0,
        le=65535,
        description="Keepalive interval in seconds (0 to disable)",
    )
    status: WireGuardPeerStatus = WireGuardPeerStatus.active
    notes: str | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class WireGuardPeerCreate(WireGuardPeerBase):
    """Create a new WireGuard peer.

    If no keys are provided, they will be auto-generated.
    The private key is returned only during creation for device configuration.
    """

    server_id: UUID
    # Keys are optional - auto-generated if not provided
    public_key: str | None = None
    private_key: str | None = None
    use_preshared_key: bool = Field(
        default=True,
        description="Generate a preshared key for post-quantum security",
    )


class WireGuardPeerUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    allowed_ips: list[str] | None = None
    peer_address: str | None = None
    peer_address_v6: str | None = None
    persistent_keepalive: int | None = Field(default=None, ge=0, le=65535)
    status: WireGuardPeerStatus | None = None
    notes: str | None = None
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class WireGuardPeerRead(WireGuardPeerBase):
    """Peer response schema (excludes private keys for security)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    server_id: UUID
    public_key: str
    has_private_key: bool = False
    has_preshared_key: bool = False

    # Connection tracking
    last_handshake_at: datetime | None = None
    endpoint_ip: str | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0

    # Provisioning
    has_provision_token: bool = False
    provision_token_expires_at: datetime | None = None

    created_at: datetime
    updated_at: datetime

    # Related entity names for display
    server_name: str | None = None


class WireGuardPeerCreated(WireGuardPeerRead):
    """Response when peer is created - includes private key (only time it's shown)."""

    private_key: str | None = None
    preshared_key: str | None = None
    provision_token: str | None = None  # Plain token (not hash)


class WireGuardPeerConfig(BaseModel):
    """WireGuard peer configuration for device setup."""

    peer_name: str
    server_name: str
    config_content: str  # Full [Interface] + [Peer] config
    filename: str  # Suggested filename


# ============== MikroTik Script Schemas ==============


class MikroTikScriptResponse(BaseModel):
    """RouterOS 7 script for WireGuard tunnel setup."""

    peer_name: str
    server_name: str
    script_content: str
    filename: str


# ============== Connection Log Schemas ==============


class WireGuardConnectionLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    peer_id: UUID
    connected_at: datetime
    disconnected_at: datetime | None = None
    endpoint_ip: str | None = None
    peer_address: str | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    disconnect_reason: str | None = None
    peer_name: str | None = None


# ============== Provisioning Schemas ==============


class GenerateProvisionTokenRequest(BaseModel):
    """Request to generate a new provisioning token."""

    expires_in_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Token validity in hours (1-168, default 24)",
    )


class ProvisionTokenResponse(BaseModel):
    """Response containing the provisioning token."""

    token: str
    expires_at: datetime
    provision_url: str


class ProvisionWithTokenRequest(BaseModel):
    """Device self-registration request using provisioning token."""

    token: str
    public_key: str = Field(
        description="Device's WireGuard public key (base64, 44 chars)"
    )


# ============== Server Stats ==============


class WireGuardServerStatus(BaseModel):
    """Server status and statistics."""

    server_id: UUID
    server_name: str
    is_active: bool
    total_peers: int
    active_peers: int
    connected_peers: int  # Peers with recent handshake
    total_rx_bytes: int
    total_tx_bytes: int
