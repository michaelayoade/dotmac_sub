"""Device state resolver SOT.

One place to interpret operator state, native polling, warmed topology status,
and pollability. Pollers write observations; this service resolves the state
other modules should consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services.common import coerce_uuid
from app.services.infrastructure_polling import pollable_device_criteria
from app.services.topology.live_status import (
    DOWN,
    PROBLEM,
    STALE_POLL_AFTER_SECONDS,
    UNKNOWN,
    UP,
    derive_live_status,
)


@dataclass(frozen=True)
class DeviceState:
    device_id: object
    name: str | None
    administrative_status: str | None
    live_status: str
    source: str
    pollable: bool
    stale: bool
    last_seen_at: datetime | None

    @property
    def is_up(self) -> bool:
        return self.live_status == UP

    @property
    def is_down(self) -> bool:
        return self.live_status in {DOWN, PROBLEM}


def _as_aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _is_stale(
    value: datetime | None, *, now: datetime, stale_after_seconds: int
) -> bool:
    value = _as_aware(value)
    if value is None:
        return True
    return (now - value).total_seconds() > stale_after_seconds


def resolve_device_state(
    db: Session,
    device: NetworkDevice | str,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> DeviceState:
    device_obj = (
        device
        if isinstance(device, NetworkDevice)
        else db.get(NetworkDevice, coerce_uuid(device))
    )
    if device_obj is None:
        raise ValueError("network device not found")
    now = now or datetime.now(UTC)
    pollable = _is_pollable(device_obj)
    if device_obj.status == DeviceStatus.maintenance:
        live_status = UNKNOWN
        source = "admin.maintenance"
    elif device_obj.live_status:
        live_status = device_obj.live_status
        source = "topology.live_status"
    elif pollable:
        live_status = derive_live_status(
            device_obj,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
        source = "native_poll_columns"
    else:
        live_status = UNKNOWN
        source = "unpollable"
    last_seen_at = (
        device_obj.last_ping_at or device_obj.last_snmp_at or device_obj.live_status_at
    )
    return DeviceState(
        device_id=device_obj.id,
        name=device_obj.name,
        administrative_status=getattr(
            device_obj.status,
            "value",
            str(device_obj.status),
        ),
        live_status=live_status,
        source=source,
        pollable=pollable,
        stale=_is_stale(
            last_seen_at,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
        if pollable
        else True,
        last_seen_at=last_seen_at,
    )


def _is_pollable(device: NetworkDevice) -> bool:
    return (
        bool(device.is_active)
        and bool(device.ping_enabled or device.snmp_enabled)
        and bool(device.mgmt_ip or device.hostname)
    )


def pollable_devices_query(db: Session):
    return db.query(NetworkDevice).filter(*pollable_device_criteria())
