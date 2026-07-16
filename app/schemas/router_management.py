import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JumpHostCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    ssh_key: str | None = None
    ssh_password: str | None = None


class JumpHostUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_key: str | None = None
    ssh_password: str | None = None
    is_active: bool | None = None


class JumpHostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    hostname: str
    port: int
    username: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RouterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    hostname: str = Field(min_length=1, max_length=255)
    management_ip: str = Field(min_length=1, max_length=255)
    rest_api_port: int = Field(default=443, ge=1, le=65535)
    rest_api_username: str = Field(min_length=1, max_length=512)
    rest_api_password: str = Field(min_length=1, max_length=512)
    use_ssl: bool = True
    verify_tls: bool = False
    location: str | None = None
    notes: str | None = None
    tags: dict | None = None
    access_method: str = "direct"
    jump_host_id: uuid.UUID | None = None
    nas_device_id: uuid.UUID | None = None
    network_device_id: uuid.UUID | None = None


class RouterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    management_ip: str | None = Field(default=None, min_length=1, max_length=255)
    rest_api_port: int | None = Field(default=None, ge=1, le=65535)
    rest_api_username: str | None = Field(default=None, min_length=1, max_length=512)
    rest_api_password: str | None = Field(default=None, min_length=1, max_length=512)
    use_ssl: bool | None = None
    verify_tls: bool | None = None
    location: str | None = None
    notes: str | None = None
    tags: dict | None = None
    access_method: str | None = None
    jump_host_id: uuid.UUID | None = None
    nas_device_id: uuid.UUID | None = None
    network_device_id: uuid.UUID | None = None
    status: str | None = None


class RouterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    hostname: str
    management_ip: str
    rest_api_port: int
    use_ssl: bool
    verify_tls: bool
    routeros_version: str | None
    board_name: str | None
    architecture: str | None
    serial_number: str | None
    firmware_type: str | None
    location: str | None
    notes: str | None
    tags: dict | None
    access_method: str
    jump_host_id: uuid.UUID | None
    nas_device_id: uuid.UUID | None
    network_device_id: uuid.UUID | None
    status: str
    last_seen_at: datetime | None
    last_config_sync_at: datetime | None
    last_config_change_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RouterInterfaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    router_id: uuid.UUID
    name: str
    type: str
    mac_address: str | None
    is_running: bool
    is_disabled: bool
    rx_byte: int
    tx_byte: int
    rx_packet: int
    tx_packet: int
    last_link_up_time: str | None
    speed: str | None
    comment: str | None
    synced_at: datetime


class RouterConfigSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    router_id: uuid.UUID
    config_export: str
    config_hash: str
    source: str
    captured_by: uuid.UUID | None
    created_at: datetime


class RouterConfigTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    template_body: str = Field(min_length=1)
    category: str = "custom"
    variables: dict = Field(default_factory=dict)
    is_active: bool = True


class RouterConfigTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    template_body: str | None = Field(default=None, min_length=1)
    category: str | None = None
    variables: dict | None = None
    is_active: bool | None = None


class RouterConfigTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    template_body: str
    category: str
    variables: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime


class RouterSotIntentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: Literal[
        "firewall_address_list",
        "firewall_filter",
        "firewall_nat",
        "simple_queue",
        "ipv4_address",
        "ipv6_address",
        "ipv4_route",
        "ipv6_route",
        "bgp_connection",
        "ospf_instance",
        "ospf_area",
        "ospf_interface_template",
        "routing_filter_rule",
    ]
    key: str = Field(min_length=1, max_length=127)
    state: Literal["present", "absent"] = "present"
    values: dict[str, str | int | bool] = Field(default_factory=dict)


class RouterConfigPushCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID | None = None
    desired_state: list[RouterSotIntentInput] = Field(min_length=1)
    variable_values: dict | None = None
    router_ids: list[uuid.UUID] = Field(min_length=1)
    dry_run: bool = False
    failure_policy: Literal["continue", "abort"] = "continue"
    allow_dangerous_commands: bool = False


class RouterConfigPushRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID | None
    commands: list
    desired_state: list[dict]
    variable_values: dict | None
    dry_run: bool
    failure_policy: str
    allow_dangerous_commands: bool
    initiated_by: uuid.UUID
    operation_id: uuid.UUID | None
    status: str
    created_at: datetime
    completed_at: datetime | None


class RouterConfigPushResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    push_id: uuid.UUID
    router_id: uuid.UUID
    operation_id: uuid.UUID | None
    status: str
    response_data: dict | list | None
    error_message: str | None
    pre_snapshot_id: uuid.UUID | None
    post_snapshot_id: uuid.UUID | None
    duration_ms: int | None
    created_at: datetime


class RouterHealthRead(BaseModel):
    cpu_load: int
    free_memory: int
    total_memory: int
    uptime: str
    free_hdd_space: int
    total_hdd_space: int
    architecture_name: str
    board_name: str
    version: str


class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    response_time_ms: int | None = None
