from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class ZabbixHostInterface(BaseModel):
    ip: str | None = None
    dns: str | None = None
    port: int | None = None
    type: int | None = None
    main: bool = False
    use_ip: bool = False


class ZabbixHostGroup(BaseModel):
    id: str
    name: str


class ZabbixHostRead(BaseModel):
    id: str
    host: str
    name: str
    enabled: bool
    available: bool | None = None
    groups: list[ZabbixHostGroup] = []
    interfaces: list[ZabbixHostInterface] = []


class ZabbixMetricRead(BaseModel):
    metric: str
    value: float
    unit: Literal["Mbps"] = "Mbps"
    timestamp: int
    source: Literal["zabbix"] = "zabbix"


class ZabbixAlertRead(BaseModel):
    trigger_id: str
    host_id: str | None = None
    host_name: str | None = None
    description: str
    priority: int
    severity: str
    status: str
    state: str
    last_change: datetime | None = None


class ZabbixHostsResponse(BaseModel):
    items: list[ZabbixHostRead]


class ZabbixMetricsResponse(BaseModel):
    items: list[ZabbixMetricRead]


class ZabbixAlertsResponse(BaseModel):
    items: list[ZabbixAlertRead]


ZabbixObject = dict[str, Any]
