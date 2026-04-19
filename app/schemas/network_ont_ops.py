"""Schemas for ONT operational endpoints — actions, enriched reads, writes, features."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ── Action request/response schemas ────────────────────────────────────


class OntActionResponse(BaseModel):
    """Standard response for ONT remote actions."""

    success: bool
    message: str
    data: dict[str, Any] | None = None


class OntWifiSsidRequest(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)


class OntWifiPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=63)


class OntLanPortToggleRequest(BaseModel):
    enabled: bool


class OntPPPoERequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=64)


class OntPingRequest(BaseModel):
    target: str = Field(min_length=1, max_length=253)
    count: int = Field(default=4, ge=1, le=20)


class OntTracerouteRequest(BaseModel):
    target: str = Field(min_length=1, max_length=253)


class OntConnectionRequestCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=64)
    periodic_inform_interval: int = Field(default=3600, ge=60, le=86400)


class OntFirmwareRequest(BaseModel):
    firmware_image_id: str


# ── Enriched read schemas ──────────────────────────────────────────────


class OntEnrichedRead(BaseModel):
    """Enriched ONT detail composing DB + signal + subscriber + capabilities."""

    model_config = ConfigDict(from_attributes=True)

    # Core identity
    id: UUID
    serial_number: str | None = None
    vendor: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    online_status: str | None = None
    acs_status: str | None = None
    effective_status: str | None = None
    effective_status_source: str | None = None
    acs_last_inform_at: datetime | None = None
    name: str | None = None

    # Signal
    olt_rx_signal_dbm: float | None = None
    onu_rx_signal_dbm: float | None = None
    signal_quality: str | None = None  # good / warning / critical
    distance_meters: float | None = None
    signal_updated_at: datetime | None = None

    # Assignment context
    subscriber_id: UUID | None = None
    subscriber_name: str | None = None
    subscription_id: UUID | None = None
    pon_port_name: str | None = None
    olt_name: str | None = None

    # Provisioning
    provisioning_status: str | None = None
    provisioning_profile_name: str | None = None

    # Observed runtime (TR-069 / polling)
    observed_wan_ip: str | None = None
    observed_pppoe_status: str | None = None
    observed_runtime_updated_at: datetime | None = None

    # Capabilities
    capabilities: dict[str, bool] = {}

    # Sync metadata
    last_sync_source: str | None = None
    last_sync_at: datetime | None = None


# ── Write schemas ──────────────────────────────────────────────────────


class OntSpeedProfileUpdate(BaseModel):
    download_profile_id: str | None = None
    upload_profile_id: str | None = None


class OntWanConfigUpdate(BaseModel):
    wan_mode: str  # dhcp, static_ip, pppoe, bridge
    vlan_id: str | None = None
    pppoe_username: str | None = None
    pppoe_password: str | None = None


class OntMgmtIpUpdate(BaseModel):
    mgmt_ip_mode: str  # inactive, static_ip, dhcp
    mgmt_vlan_id: str | None = None
    mgmt_ip_address: str | None = None
    mgmt_subnet: str | None = None
    mgmt_gateway: str | None = None


class OntServicePortUpdate(BaseModel):
    vlan_id: int
    gem_index: int
    user_vlan: int | None = None
    tag_transform: str = "translate"


class OntMoveRequest(BaseModel):
    target_pon_port_id: str


class OntExternalIdUpdate(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)


# ── Feature schemas ────────────────────────────────────────────────────


class OntWifiConfigRequest(BaseModel):
    ssid: str | None = Field(default=None, max_length=32)
    password: str | None = Field(default=None, min_length=8, max_length=63)
    enabled: bool | None = None
    band: str | None = None  # 2.4ghz, 5ghz


class OntFeatureToggleRequest(BaseModel):
    enabled: bool


class OntMaxMacLearnRequest(BaseModel):
    max_mac: int = Field(ge=1, le=128)


class OntWebCredentialsRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=64)


# ── Bulk operation schemas ─────────────────────────────────────────────


class OntBulkActionRequest(BaseModel):
    ont_ids: list[str] = Field(min_length=1, max_length=200)
    action: str  # reboot, factory_reset, speed_update, catv_toggle, wifi_update, voip_toggle, provision_saga
    params: dict[str, Any] = {}


class OntBulkActionResponse(BaseModel):
    task_id: str
    message: str


class OntBulkActionStatus(BaseModel):
    task_id: str
    status: str  # PENDING, STARTED, SUCCESS, FAILURE
    result: dict[str, int] | None = None
