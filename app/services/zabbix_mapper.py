from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.zabbix import (
    ZabbixAlertRead,
    ZabbixHostGroup,
    ZabbixHostInterface,
    ZabbixHostRead,
    ZabbixObject,
)

_SEVERITY = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_from_int(value: Any) -> bool:
    return str(value) == "1"


def _timestamp(value: Any) -> datetime | None:
    number = _int(value)
    if not number:
        return None
    return datetime.fromtimestamp(number, tz=UTC)


def map_host(raw: ZabbixObject) -> ZabbixHostRead:
    groups = [
        ZabbixHostGroup(id=_str(group.get("groupid")), name=_str(group.get("name")))
        for group in raw.get("groups", [])
        if isinstance(group, dict)
    ]
    interfaces = []
    for item in raw.get("interfaces", []):
        if not isinstance(item, dict):
            continue
        interfaces.append(
            ZabbixHostInterface(
                ip=_str(item.get("ip")) or None,
                dns=_str(item.get("dns")) or None,
                port=_int(item.get("port")),
                type=_int(item.get("type")),
                main=_bool_from_int(item.get("main")),
                use_ip=_bool_from_int(item.get("useip")),
            )
        )

    available = _int(raw.get("available"))
    return ZabbixHostRead(
        id=_str(raw.get("hostid")),
        host=_str(raw.get("host")),
        name=_str(raw.get("name")),
        enabled=str(raw.get("status")) == "0",
        available=None if available is None else available == 1,
        groups=groups,
        interfaces=interfaces,
    )


def map_alert(raw: ZabbixObject) -> ZabbixAlertRead:
    priority = _int(raw.get("priority")) or 0
    hosts = raw.get("hosts") if isinstance(raw.get("hosts"), list) else []
    host = hosts[0] if hosts and isinstance(hosts[0], dict) else {}
    return ZabbixAlertRead(
        trigger_id=_str(raw.get("triggerid")),
        host_id=_str(host.get("hostid")) or None,
        host_name=_str(host.get("name") or host.get("host")) or None,
        description=_str(raw.get("description")),
        priority=priority,
        severity=_SEVERITY.get(priority, "unknown"),
        status="enabled" if str(raw.get("status")) == "0" else "disabled",
        state="problem" if str(raw.get("value")) == "1" else "ok",
        last_change=_timestamp(raw.get("lastchange")),
    )


def map_hosts(raw_items: list[ZabbixObject]) -> list[ZabbixHostRead]:
    return [map_host(item) for item in raw_items if isinstance(item, dict)]


def map_alerts(raw_items: list[ZabbixObject]) -> list[ZabbixAlertRead]:
    return [map_alert(item) for item in raw_items if isinstance(item, dict)]
