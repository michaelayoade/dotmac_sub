"""Shared ONT status resolution across OLT polling and ACS/TR-069 freshness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil

from app.models.network import OntAcsStatus, OntStatusSource, OntUnit, OnuOnlineStatus

DEFAULT_ACS_ONLINE_WINDOW_MINUTES = 15
ACS_INFORM_GRACE_MINUTES = 5


@dataclass(frozen=True)
class OntStatusSnapshot:
    olt_status: OnuOnlineStatus
    acs_status: OntAcsStatus
    acs_last_inform_at: datetime | None
    effective_status: OnuOnlineStatus
    effective_status_source: OntStatusSource
    status_resolved_at: datetime


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _normalize_olt_status(value: OnuOnlineStatus | str | None) -> OnuOnlineStatus:
    if isinstance(value, OnuOnlineStatus):
        return value
    if isinstance(value, str):
        try:
            return OnuOnlineStatus(value)
        except ValueError:
            return OnuOnlineStatus.unknown
    return OnuOnlineStatus.unknown


def _window_minutes_from_interval_seconds(interval_seconds: int | None) -> int:
    if not interval_seconds or interval_seconds <= 0:
        return DEFAULT_ACS_ONLINE_WINDOW_MINUTES
    interval_minutes = ceil(interval_seconds / 60)
    return max(
        DEFAULT_ACS_ONLINE_WINDOW_MINUTES,
        interval_minutes + ACS_INFORM_GRACE_MINUTES,
    )


def resolve_acs_online_window_minutes_for_model(ont: OntUnit) -> int:
    interval_seconds = None

    acs_server = getattr(ont, "tr069_acs_server", None)
    if acs_server is not None:
        interval_seconds = getattr(acs_server, "periodic_inform_interval", None)

    if interval_seconds is None:
        olt = getattr(ont, "olt_device", None)
        if olt is not None:
            olt_acs_server = getattr(olt, "tr069_acs_server", None)
            if olt_acs_server is not None:
                interval_seconds = getattr(
                    olt_acs_server, "periodic_inform_interval", None
                )

    return _window_minutes_from_interval_seconds(interval_seconds)


def ont_has_acs_management(
    ont: OntUnit,
    *,
    acs_last_inform_at: datetime | None = None,
) -> bool:
    if getattr(ont, "tr069_acs_server_id", None) or getattr(ont, "tr069_acs_server", None):
        return True

    olt = getattr(ont, "olt_device", None)
    if olt is not None and (
        getattr(olt, "tr069_acs_server_id", None) or getattr(olt, "tr069_acs_server", None)
    ):
        return True

    return bool(acs_last_inform_at or getattr(ont, "acs_last_inform_at", None))


def resolve_acs_status(
    *,
    acs_last_inform_at: datetime | None,
    managed: bool | None = None,
    now: datetime | None = None,
    online_window_minutes: int = DEFAULT_ACS_ONLINE_WINDOW_MINUTES,
) -> OntAcsStatus:
    current = now or datetime.now(UTC)
    last_inform = _normalize_timestamp(acs_last_inform_at)
    if managed is False:
        return OntAcsStatus.unmanaged
    if last_inform is None:
        return OntAcsStatus.unknown
    if last_inform >= current - timedelta(minutes=online_window_minutes):
        return OntAcsStatus.online
    return OntAcsStatus.stale


def resolve_ont_status_snapshot(
    *,
    olt_status: OnuOnlineStatus | str | None,
    acs_last_inform_at: datetime | None,
    managed: bool | None = None,
    now: datetime | None = None,
    online_window_minutes: int = DEFAULT_ACS_ONLINE_WINDOW_MINUTES,
) -> OntStatusSnapshot:
    current = now or datetime.now(UTC)
    normalized_olt = _normalize_olt_status(olt_status)
    normalized_inform = _normalize_timestamp(acs_last_inform_at)
    acs_status = resolve_acs_status(
        acs_last_inform_at=normalized_inform,
        managed=managed,
        now=current,
        online_window_minutes=online_window_minutes,
    )

    if normalized_olt == OnuOnlineStatus.online:
        effective_status = OnuOnlineStatus.online
        source = OntStatusSource.olt
    elif acs_status == OntAcsStatus.online:
        effective_status = OnuOnlineStatus.online
        source = OntStatusSource.acs
    elif normalized_olt == OnuOnlineStatus.offline:
        effective_status = OnuOnlineStatus.offline
        source = OntStatusSource.olt
    else:
        effective_status = OnuOnlineStatus.unknown
        source = OntStatusSource.derived

    return OntStatusSnapshot(
        olt_status=normalized_olt,
        acs_status=acs_status,
        acs_last_inform_at=normalized_inform,
        effective_status=effective_status,
        effective_status_source=source,
        status_resolved_at=current,
    )


def resolve_ont_status_for_model(
    ont: OntUnit,
    *,
    acs_last_inform_at: datetime | None = None,
    now: datetime | None = None,
    online_window_minutes: int | None = None,
) -> OntStatusSnapshot:
    effective_inform = (
        acs_last_inform_at
        if acs_last_inform_at is not None
        else getattr(ont, "acs_last_inform_at", None)
    )
    managed = ont_has_acs_management(ont, acs_last_inform_at=effective_inform)
    return resolve_ont_status_snapshot(
        olt_status=getattr(ont, "online_status", None),
        acs_last_inform_at=effective_inform,
        managed=managed,
        now=now,
        online_window_minutes=(
            online_window_minutes
            if online_window_minutes is not None
            else resolve_acs_online_window_minutes_for_model(ont)
        ),
    )


def apply_status_snapshot(ont: OntUnit, snapshot: OntStatusSnapshot) -> OntUnit:
    ont.acs_status = snapshot.acs_status
    ont.acs_last_inform_at = snapshot.acs_last_inform_at
    ont.effective_status = snapshot.effective_status
    ont.effective_status_source = snapshot.effective_status_source
    ont.status_resolved_at = snapshot.status_resolved_at
    return ont
