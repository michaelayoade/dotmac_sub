"""Pure ONT status resolution and persistence helpers."""

from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from math import ceil

from sqlalchemy.orm import Session

from app.models.network import (
    OntAuthorizationStatus,
    OntProvisioningStatus,
    OntStatusSource,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
)

DEFAULT_ACS_ONLINE_WINDOW_MINUTES = 15
ACS_INFORM_GRACE_MINUTES = 5
OFFLINE_POLL_THRESHOLD = 3

logger = logging.getLogger(__name__)

_AUTHORIZATION_TRANSITIONS: dict[
    OntAuthorizationStatus | None, set[OntAuthorizationStatus]
] = {
    None: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.pending: {
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.authorized: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.deauthorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.deauthorized: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.failed,
    },
    OntAuthorizationStatus.failed: {
        OntAuthorizationStatus.pending,
        OntAuthorizationStatus.authorized,
        OntAuthorizationStatus.deauthorized,
    },
}

_PROVISIONING_TRANSITIONS: dict[
    OntProvisioningStatus | None, set[OntProvisioningStatus]
] = {
    None: {
        OntProvisioningStatus.unprovisioned,
        OntProvisioningStatus.partial,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
    },
    OntProvisioningStatus.unprovisioned: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
    },
    OntProvisioningStatus.partial: {
        OntProvisioningStatus.provisioned,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.provisioned: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.failed,
        OntProvisioningStatus.unprovisioned,
    },
    OntProvisioningStatus.failed: {
        OntProvisioningStatus.partial,
        OntProvisioningStatus.unprovisioned,
        OntProvisioningStatus.provisioned,
    },
}


@dataclass(frozen=True)
class OntStatusInputs:
    olt_status: OnuOnlineStatus
    olt_seen_at: datetime | None
    acs_last_inform_at: datetime | None
    acs_online_window_minutes: int
    consecutive_offline_polls: int


@dataclass(frozen=True)
class OntStatusResolution:
    effective_status: OnuOnlineStatus
    effective_status_source: OntStatusSource
    last_seen_at: datetime | None
    consecutive_offline_polls: int


@dataclass(frozen=True)
class OntStatusSnapshot:
    olt_status: OnuOnlineStatus
    olt_status_seen_at: datetime | None
    acs_last_inform_at: datetime | None
    effective_status: OnuOnlineStatus
    effective_status_source: OntStatusSource
    last_seen_at: datetime | None
    consecutive_offline_polls: int


@dataclass(frozen=True)
class OntStateReconciliationResult:
    """Outcome of reconciling OLT and ACS status observations."""

    ont_id: uuid.UUID
    snapshot: OntStatusSnapshot
    conflict: bool
    reason: str
    authoritative_source: OntStatusSource
    recommended_action: str | None = None


class StatusProviderMode(str, Enum):
    auto = "auto"
    snmp = "snmp"
    tr069 = "tr069"


@dataclass(frozen=True)
class OpticalMetrics:
    olt_rx_dbm: float | None = None
    onu_rx_dbm: float | None = None
    onu_tx_dbm: float | None = None
    temperature_c: float | None = None
    voltage_v: float | None = None
    bias_current_ma: float | None = None
    distance_m: int | None = None
    source: str = "persisted"
    fetched_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "olt_rx_dbm": self.olt_rx_dbm,
            "onu_rx_dbm": self.onu_rx_dbm,
            "onu_tx_dbm": self.onu_tx_dbm,
            "temperature_c": self.temperature_c,
            "voltage_v": self.voltage_v,
            "bias_current_ma": self.bias_current_ma,
            "distance_m": self.distance_m,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }

    @property
    def has_signal_data(self) -> bool:
        return any(
            value is not None
            for value in (self.olt_rx_dbm, self.onu_rx_dbm, self.onu_tx_dbm)
        )


@dataclass(frozen=True)
class OntStatusResult:
    effective_status: OnuOnlineStatus
    status_source: OntStatusSource
    acs_last_inform_at: datetime | None = None
    resolved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    optical_metrics: OpticalMetrics | None = None
    error: str | None = None

    @property
    def is_online(self) -> bool:
        return self.effective_status == OnuOnlineStatus.online

    @property
    def success(self) -> bool:
        return self.error is None


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


def _coerce_auth_status(
    status: OntAuthorizationStatus | str,
) -> OntAuthorizationStatus:
    if isinstance(status, OntAuthorizationStatus):
        return status
    return OntAuthorizationStatus(str(status))


def _coerce_provisioning_status(
    status: OntProvisioningStatus | str,
) -> OntProvisioningStatus:
    if isinstance(status, OntProvisioningStatus):
        return status
    return OntProvisioningStatus(str(status))


def set_authorization_status(
    ont: OntUnit,
    status: OntAuthorizationStatus | str,
    *,
    strict: bool = True,
) -> None:
    next_status = _coerce_auth_status(status)
    current = ont.authorization_status
    if current == next_status:
        return
    allowed = _AUTHORIZATION_TRANSITIONS.get(current, set())
    is_valid_transition = next_status in allowed
    if not is_valid_transition:
        message = (
            f"Illegal ONT authorization status transition: "
            f"{current.value if current else 'none'} -> {next_status.value}"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "authorization_status",
            "from": current.value if current else None,
            "to": next_status.value,
            "valid": is_valid_transition,
        },
    )
    ont.authorization_status = next_status


def set_provisioning_status(
    ont: OntUnit,
    status: OntProvisioningStatus | str,
    *,
    strict: bool = True,
) -> None:
    next_status = _coerce_provisioning_status(status)
    current = ont.provisioning_status
    if current == next_status:
        return
    allowed = _PROVISIONING_TRANSITIONS.get(current, set())
    is_valid_transition = next_status in allowed
    if not is_valid_transition:
        message = (
            f"Illegal ONT provisioning status transition: "
            f"{current.value if current else 'none'} -> {next_status.value}"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)
    logger.info(
        "ont_status_transition",
        extra={
            "event": "ont_status_transition",
            "ont_id": str(ont.id),
            "field": "provisioning_status",
            "from": current.value if current else None,
            "to": next_status.value,
            "valid": is_valid_transition,
        },
    )
    ont.provisioning_status = next_status


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
    if getattr(ont, "tr069_acs_server_id", None) or getattr(
        ont, "tr069_acs_server", None
    ):
        return True
    olt = getattr(ont, "olt_device", None)
    if olt is not None and (
        getattr(olt, "tr069_acs_server_id", None)
        or getattr(olt, "tr069_acs_server", None)
    ):
        return True
    return bool(acs_last_inform_at or getattr(ont, "acs_last_inform_at", None))


def resolve_effective_last_seen_at(
    ont: OntUnit | object,
    *,
    acs_last_inform_at: datetime | None = None,
) -> datetime | None:
    candidates = [
        _normalize_timestamp(getattr(ont, "last_seen_at", None)),
        _normalize_timestamp(getattr(ont, "olt_status_seen_at", None)),
        _normalize_timestamp(
            acs_last_inform_at
            if acs_last_inform_at is not None
            else getattr(ont, "acs_last_inform_at", None)
        ),
    ]
    present = [value for value in candidates if value is not None]
    return max(present) if present else None


def resolve_ont_effective_status(
    inputs: OntStatusInputs,
    *,
    now: datetime | None = None,
) -> OntStatusResolution:
    current = now or datetime.now(UTC)
    olt_seen_at = _normalize_timestamp(inputs.olt_seen_at)
    acs_last_inform_at = _normalize_timestamp(inputs.acs_last_inform_at)
    acs_is_recent = (
        acs_last_inform_at is not None
        and acs_last_inform_at
        >= current - timedelta(minutes=inputs.acs_online_window_minutes)
    )

    if acs_is_recent:
        status = OnuOnlineStatus.online
        source = OntStatusSource.acs
    elif inputs.olt_status == OnuOnlineStatus.online:
        status = OnuOnlineStatus.online
        source = OntStatusSource.olt
    elif (
        inputs.olt_status == OnuOnlineStatus.offline
        and inputs.consecutive_offline_polls >= OFFLINE_POLL_THRESHOLD
    ):
        status = OnuOnlineStatus.offline
        source = OntStatusSource.olt
    else:
        status = OnuOnlineStatus.unknown
        source = OntStatusSource.derived

    last_seen_candidates = [
        olt_seen_at if inputs.olt_status == OnuOnlineStatus.online else None,
        acs_last_inform_at,
    ]
    last_seen = max([value for value in last_seen_candidates if value is not None], default=None)
    return OntStatusResolution(
        effective_status=status,
        effective_status_source=source,
        last_seen_at=last_seen,
        consecutive_offline_polls=inputs.consecutive_offline_polls,
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
    inputs = OntStatusInputs(
        olt_status=_normalize_olt_status(getattr(ont, "olt_status", None)),
        olt_seen_at=getattr(ont, "olt_status_seen_at", None),
        acs_last_inform_at=effective_inform,
        acs_online_window_minutes=(
            online_window_minutes
            if online_window_minutes is not None
            else resolve_acs_online_window_minutes_for_model(ont)
        ),
        consecutive_offline_polls=int(getattr(ont, "consecutive_offline_polls", 0) or 0),
    )
    resolution = resolve_ont_effective_status(inputs, now=now)
    return OntStatusSnapshot(
        olt_status=inputs.olt_status,
        olt_status_seen_at=_normalize_timestamp(inputs.olt_seen_at),
        acs_last_inform_at=_normalize_timestamp(effective_inform),
        effective_status=resolution.effective_status,
        effective_status_source=resolution.effective_status_source,
        last_seen_at=resolution.last_seen_at,
        consecutive_offline_polls=resolution.consecutive_offline_polls,
    )


def resolve_ont_status_snapshot(
    *,
    olt_status: OnuOnlineStatus | str | None,
    acs_last_inform_at: datetime | None,
    now: datetime | None = None,
    online_window_minutes: int = DEFAULT_ACS_ONLINE_WINDOW_MINUTES,
    consecutive_offline_polls: int = 0,
) -> OntStatusSnapshot:
    current = now or datetime.now(UTC)
    normalized_olt = _normalize_olt_status(olt_status)
    inputs = OntStatusInputs(
        olt_status=normalized_olt,
        olt_seen_at=current if normalized_olt != OnuOnlineStatus.unknown else None,
        acs_last_inform_at=acs_last_inform_at,
        acs_online_window_minutes=online_window_minutes,
        consecutive_offline_polls=consecutive_offline_polls,
    )
    resolution = resolve_ont_effective_status(inputs, now=current)
    return OntStatusSnapshot(
        olt_status=normalized_olt,
        olt_status_seen_at=inputs.olt_seen_at,
        acs_last_inform_at=_normalize_timestamp(acs_last_inform_at),
        effective_status=resolution.effective_status,
        effective_status_source=resolution.effective_status_source,
        last_seen_at=resolution.last_seen_at,
        consecutive_offline_polls=resolution.consecutive_offline_polls,
    )


def apply_status_snapshot(ont: OntUnit, snapshot: OntStatusSnapshot) -> OntUnit:
    ont.olt_status = snapshot.olt_status
    ont.olt_status_seen_at = snapshot.olt_status_seen_at
    ont.acs_last_inform_at = snapshot.acs_last_inform_at
    ont.effective_status = snapshot.effective_status
    ont.effective_status_source = snapshot.effective_status_source
    ont.consecutive_offline_polls = snapshot.consecutive_offline_polls
    if snapshot.last_seen_at is not None:
        ont.last_seen_at = snapshot.last_seen_at
    return ont


def reset_status_for_inventory(ont: OntUnit) -> None:
    """Clear persisted status observations for an ONT returned to inventory."""
    ont.olt_status = OnuOnlineStatus.unknown
    ont.olt_status_seen_at = None
    ont.acs_last_inform_at = None
    ont.effective_status = OnuOnlineStatus.unknown
    ont.effective_status_source = OntStatusSource.derived
    ont.last_seen_at = None
    ont.offline_reason = None
    ont.consecutive_offline_polls = 0


def apply_olt_status_observation(
    ont: OntUnit,
    olt_status: OnuOnlineStatus,
    offline_reason: OnuOfflineReason | None = None,
    *,
    now: datetime | None = None,
) -> OntStatusSnapshot:
    current = now or datetime.now(UTC)
    normalized_status = _normalize_olt_status(olt_status)
    ont.olt_status = normalized_status
    ont.olt_status_seen_at = current

    if normalized_status == OnuOnlineStatus.online:
        ont.last_seen_at = current
        ont.offline_reason = None
        ont.consecutive_offline_polls = 0
    elif normalized_status == OnuOnlineStatus.offline:
        ont.offline_reason = offline_reason or OnuOfflineReason.unknown
        ont.consecutive_offline_polls = (ont.consecutive_offline_polls or 0) + 1
    else:
        ont.offline_reason = offline_reason

    snapshot = resolve_ont_status_for_model(ont, now=current)
    apply_status_snapshot(ont, snapshot)
    return snapshot


def apply_status_with_hysteresis(
    ont: OntUnit,
    polled_status: OnuOnlineStatus,
    offline_reason: OnuOfflineReason | None = None,
    *,
    now: datetime | None = None,
) -> None:
    apply_olt_status_observation(ont, polled_status, offline_reason, now=now)


def apply_acs_inform_observation(
    ont: OntUnit,
    *,
    acs_last_inform_at: datetime | None = None,
    now: datetime | None = None,
) -> OntStatusSnapshot:
    current = now or datetime.now(UTC)
    ont.acs_last_inform_at = _normalize_timestamp(acs_last_inform_at) or current
    snapshot = resolve_ont_status_for_model(ont, now=current)
    apply_status_snapshot(ont, snapshot)
    return snapshot


def update_ont_acs_status_from_inform(
    db: Session,
    ont: OntUnit,
    *,
    informed_at: datetime | None = None,
    commit: bool = False,
) -> OntStatusSnapshot:
    snapshot = apply_acs_inform_observation(ont, acs_last_inform_at=informed_at)
    if commit:
        db.commit()
    return snapshot


def reconcile_ont_state(ont: OntUnit, *, now: datetime | None = None) -> OntStateReconciliationResult:
    snapshot = resolve_ont_status_for_model(ont, now=now)
    conflict = (
        snapshot.olt_status == OnuOnlineStatus.offline
        and snapshot.effective_status_source == OntStatusSource.acs
    )
    return OntStateReconciliationResult(
        ont_id=ont.id,
        snapshot=snapshot,
        conflict=conflict,
        reason="recent_acs_inform_overrides_olt_offline" if conflict else "resolved",
        authoritative_source=snapshot.effective_status_source,
        recommended_action="check_olt_polling_freshness" if conflict else None,
    )


def _optical_metrics_from_ont(ont: OntUnit) -> OpticalMetrics:
    return OpticalMetrics(
        olt_rx_dbm=getattr(ont, "olt_rx_signal_dbm", None),
        onu_rx_dbm=getattr(ont, "onu_rx_signal_dbm", None),
        onu_tx_dbm=getattr(ont, "onu_tx_signal_dbm", None),
        temperature_c=getattr(ont, "ont_temperature_c", None),
        voltage_v=getattr(ont, "ont_voltage_v", None),
        bias_current_ma=getattr(ont, "ont_bias_current_ma", None),
        distance_m=getattr(ont, "distance_meters", None),
        fetched_at=getattr(ont, "signal_updated_at", None),
    )


def get_ont_status(
    db: Session,
    ont: OntUnit,
    *,
    include_optical: bool = False,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusResult:
    _ = db, mode
    snapshot = resolve_ont_status_for_model(ont)
    return OntStatusResult(
        effective_status=snapshot.effective_status,
        status_source=snapshot.effective_status_source,
        acs_last_inform_at=snapshot.acs_last_inform_at,
        resolved_at=datetime.now(UTC),
        optical_metrics=_optical_metrics_from_ont(ont) if include_optical else None,
    )


def get_optical_metrics(
    db: Session,
    ont: OntUnit,
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OpticalMetrics:
    _ = db, mode
    return _optical_metrics_from_ont(ont)


def refresh_ont_status(
    db: Session,
    ont: OntUnit,
    *,
    mode: StatusProviderMode | str = StatusProviderMode.auto,
) -> OntStatusResult:
    _ = mode
    snapshot = resolve_ont_status_for_model(ont)
    apply_status_snapshot(ont, snapshot)
    db.flush()
    return OntStatusResult(
        effective_status=snapshot.effective_status,
        status_source=snapshot.effective_status_source,
        acs_last_inform_at=snapshot.acs_last_inform_at,
        resolved_at=datetime.now(UTC),
    )
