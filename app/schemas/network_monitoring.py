from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.network_monitoring import (
    AlertOperator,
    AlertSeverity,
    AlertStatus,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    InterfaceStatus,
    MetricType,
)


class PopSiteBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool = True


class PopSiteCreate(PopSiteBase):
    pass


class PopSiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=60)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool | None = None


class PopSiteRead(PopSiteBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class NetworkDeviceBase(BaseModel):
    pop_site_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    device_type: DeviceType | None = None
    role: DeviceRole = DeviceRole.edge
    status: DeviceStatus = DeviceStatus.offline
    ping_enabled: bool = True
    snmp_enabled: bool = False
    snmp_port: int | None = None
    snmp_version: str | None = None
    snmp_community: str | None = None
    snmp_username: str | None = None
    snmp_auth_protocol: str | None = None
    snmp_auth_secret: str | None = None
    snmp_priv_protocol: str | None = None
    snmp_priv_secret: str | None = None
    notes: str | None = None
    is_active: bool = True


class NetworkDeviceCreate(NetworkDeviceBase):
    pass


class NetworkDeviceUpdate(BaseModel):
    pop_site_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    device_type: DeviceType | None = None
    role: DeviceRole | None = None
    status: DeviceStatus | None = None
    ping_enabled: bool | None = None
    snmp_enabled: bool | None = None
    snmp_port: int | None = None
    snmp_version: str | None = None
    snmp_community: str | None = None
    snmp_username: str | None = None
    snmp_auth_protocol: str | None = None
    snmp_auth_secret: str | None = None
    snmp_priv_protocol: str | None = None
    snmp_priv_secret: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class NetworkDeviceRead(NetworkDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class DeviceInterfaceBase(BaseModel):
    device_id: UUID
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=160)
    status: InterfaceStatus = InterfaceStatus.unknown
    speed_mbps: int | None = None
    mac_address: str | None = Field(default=None, max_length=64)


class DeviceInterfaceCreate(DeviceInterfaceBase):
    pass


class DeviceInterfaceUpdate(BaseModel):
    device_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=160)
    status: InterfaceStatus | None = None
    speed_mbps: int | None = None
    mac_address: str | None = Field(default=None, max_length=64)


class UptimeReportRequest(BaseModel):
    period_start: datetime
    period_end: datetime
    group_by: str = Field(default="device", pattern="^(device|pop_site|area|fdh)$")


class UptimeReportItem(BaseModel):
    group_by: str
    group_id: UUID | None
    name: str
    device_count: int
    total_seconds: int
    downtime_seconds: int
    uptime_percent: Decimal | None


class UptimeReportResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    group_by: str
    items: list[UptimeReportItem]


class DeviceInterfaceRead(DeviceInterfaceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class DeviceMetricBase(BaseModel):
    device_id: UUID
    interface_id: UUID | None = None
    metric_type: MetricType = MetricType.custom
    value: int = 0
    unit: str | None = Field(default=None, max_length=40)
    recorded_at: datetime


class DeviceMetricCreate(DeviceMetricBase):
    pass


class DeviceMetricUpdate(BaseModel):
    device_id: UUID | None = None
    interface_id: UUID | None = None
    metric_type: MetricType | None = None
    value: int | None = None
    unit: str | None = Field(default=None, max_length=40)
    recorded_at: datetime | None = None


class DeviceMetricRead(DeviceMetricBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class AlertRuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    metric_type: MetricType
    operator: AlertOperator = AlertOperator.gt
    threshold: float
    duration_seconds: int | None = Field(default=None, ge=0)
    severity: AlertSeverity = AlertSeverity.warning
    device_id: UUID | None = None
    interface_id: UUID | None = None
    is_active: bool = True
    notes: str | None = None


class AlertRuleCreate(AlertRuleBase):
    pass


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    metric_type: MetricType | None = None
    operator: AlertOperator | None = None
    threshold: float | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    severity: AlertSeverity | None = None
    device_id: UUID | None = None
    interface_id: UUID | None = None
    is_active: bool | None = None
    notes: str | None = None


class AlertRuleRead(AlertRuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AlertBase(BaseModel):
    rule_id: UUID
    device_id: UUID | None = None
    interface_id: UUID | None = None
    metric_type: MetricType
    measured_value: float
    status: AlertStatus = AlertStatus.open
    severity: AlertSeverity = AlertSeverity.warning
    triggered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class AlertRead(AlertBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class AlertEventBase(BaseModel):
    alert_id: UUID
    status: AlertStatus
    message: str | None = Field(default=None, max_length=255)


class AlertEventRead(AlertEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class AlertAcknowledgeRequest(BaseModel):
    message: str | None = Field(default=None, max_length=255)


class AlertResolveRequest(BaseModel):
    message: str | None = Field(default=None, max_length=255)


class AlertBulkAcknowledgeRequest(BaseModel):
    alert_ids: list[UUID]
    message: str | None = Field(default=None, max_length=255)


class AlertBulkResolveRequest(BaseModel):
    alert_ids: list[UUID]
    message: str | None = Field(default=None, max_length=255)


class AlertBulkActionResponse(BaseModel):
    updated: int


class AlertRuleBulkUpdateRequest(BaseModel):
    rule_ids: list[UUID]
    is_active: bool


class AlertRuleBulkUpdateResponse(BaseModel):
    updated: int
