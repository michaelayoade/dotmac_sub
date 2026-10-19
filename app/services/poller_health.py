"""Existing Redis poller projection owner and bounded operator liveness query.

The poller publishes after caught cycle failures too. This projection proves
loop progression, never successful collection or readiness.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import redis

from app.services.platform_health_observations import (
    PlatformHealthObservation,
    PlatformHealthState,
    make_platform_health_observation,
)

POLLER_COMPONENT_CODE = "sub.bandwidth_poller"
POLLER_HEALTH_KEY = os.getenv("BANDWIDTH_POLLER_HEALTH_KEY", "bandwidth:poller:health")
_MAX_PAYLOAD_BYTES = 262144
_redis_client: redis.Redis | None = None


class PollerFailureCategory(StrEnum):
    NO_ROUTE = "no_route_to_host"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication_rejected"
    REFUSED = "connection_refused"
    UNREACHABLE = "network_unreachable"
    TRANSPORT = "transport_error"


@dataclass(frozen=True)
class PollerDeviceFailure:
    device_id: UUID
    name: str
    host: str
    error_category: PollerFailureCategory
    consecutive_failures: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    next_attempt_at: datetime | None
    services_without_live_bandwidth: int


@dataclass(frozen=True, slots=True)
class PollerObservationIdentity:
    """Native cycle coordinates only; not an authenticated deployment identity."""

    poll_count: int
    observed_at: datetime


@dataclass(frozen=True)
class PollerSnapshot:
    observed_at: datetime
    cycle_seconds: float
    poll_count: int
    devices_total: int
    devices_ok: int
    devices_failing: int
    device_failures: tuple[PollerDeviceFailure, ...] = ()

    @property
    def observation_identity(self) -> PollerObservationIdentity:
        """Retain the source timestamp verbatim, independently of outcome/freshness."""
        return PollerObservationIdentity(self.poll_count, self.observed_at)


def poller_observation_identity(
    *, snapshot: PollerSnapshot | None
) -> PollerObservationIdentity | None:
    """Missing/malformed/unavailable source snapshots have no native coordinates."""
    return None if snapshot is None else snapshot.observation_identity


def _nonnegative_count(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 2**63 - 1
    ):
        raise ValueError("invalid counter")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return result


def _optional_timestamp(value: object) -> datetime | None:
    return None if value is None else _timestamp(value)


def _text(value: object, *, limit: int = 1024) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid text")
    return value


def decode_poller_health(*, raw: bytes | str) -> PollerSnapshot | None:
    """Normalize the existing wire format once; malformed input has no authority."""
    if len(raw) > _MAX_PAYLOAD_BYTES:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        observed_at = _timestamp(data.get("ts"))
        duration = data.get("cycle_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (float, int))
            or not 0 <= duration <= 86400
        ):
            return None
        cycle_seconds = float(duration)
        if not math.isfinite(cycle_seconds):
            return None
        total = _nonnegative_count(data.get("devices_total"))
        ok = _nonnegative_count(data.get("devices_ok"))
        failing = _nonnegative_count(data.get("devices_failing"))
        if ok + failing != total:
            return None
        rows = data.get("device_failures", [])
        if not isinstance(rows, list) or len(rows) > 100:
            return None
        failures: list[PollerDeviceFailure] = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            failures.append(
                PollerDeviceFailure(
                    device_id=UUID(_text(row.get("device_id"), limit=36)),
                    name=_text(row.get("name")),
                    host=_text(row.get("host")),
                    error_category=PollerFailureCategory(
                        _text(row.get("error_category"))
                    ),
                    consecutive_failures=_nonnegative_count(
                        row.get("consecutive_failures")
                    ),
                    last_attempt_at=_optional_timestamp(row.get("last_attempt_at")),
                    last_success_at=_optional_timestamp(row.get("last_success_at")),
                    next_attempt_at=_optional_timestamp(row.get("next_attempt_at")),
                    services_without_live_bandwidth=_nonnegative_count(
                        row.get("services_without_live_bandwidth")
                    ),
                )
            )
        return PollerSnapshot(
            observed_at,
            cycle_seconds,
            _nonnegative_count(data.get("poll_count")),
            total,
            ok,
            failing,
            tuple(failures),
        )
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def _get_redis() -> redis.Redis | None:
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        _redis_client = redis.Redis.from_url(
            url, socket_timeout=2, socket_connect_timeout=2
        )
    return _redis_client


def load_poller_health() -> PollerSnapshot | None:
    """Read the incumbent key with bounded payload/I/O; scrape callers never raise."""
    try:
        client = _get_redis()
        if client is None:
            return None
        # GETRANGE bounds bytes in Redis itself; one extra byte detects oversize.
        raw = client.getrange(POLLER_HEALTH_KEY, 0, _MAX_PAYLOAD_BYTES)
        if not isinstance(raw, (bytes, str)) or not raw:
            return None
        return decode_poller_health(raw=raw)
    except Exception:
        return None


class PollerHealthReason(StrEnum):
    LIVE = "live"
    UNAVAILABLE = "unavailable"
    FUTURE = "future"
    STALE = "stale"


@dataclass(frozen=True)
class PollerLivenessQuery:
    """Budget includes the configured poll interval and bounded cycle duration."""

    max_age_seconds: float
    now: datetime

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_age_seconds, bool)
            or not 0 < self.max_age_seconds <= 3600
            or not math.isfinite(self.max_age_seconds)
        ):
            raise ValueError("max_age_seconds must be in (0, 3600]")
        if self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")


@dataclass(frozen=True)
class PollerLiveness:
    reason: PollerHealthReason
    observed_at: datetime | None = None
    age_seconds: float | None = None
    devices_failing: int | None = None

    @property
    def live(self) -> bool:
        return self.reason is PollerHealthReason.LIVE


def _classify_poller_snapshot(
    *, snapshot: PollerSnapshot | None, query: PollerLivenessQuery
) -> PollerLiveness:
    if snapshot is None:
        return PollerLiveness(reason=PollerHealthReason.UNAVAILABLE)
    age = (query.now - snapshot.observed_at).total_seconds()
    reason = PollerHealthReason.LIVE
    if age < 0:
        reason = PollerHealthReason.FUTURE
    elif age > query.max_age_seconds:
        reason = PollerHealthReason.STALE
    return PollerLiveness(reason, snapshot.observed_at, age, snapshot.devices_failing)


def query_poller_liveness(*, query: PollerLivenessQuery) -> PollerLiveness:
    """Fresh failed/idle cycles are live; successful-cycle provenance is absent."""
    return _classify_poller_snapshot(snapshot=load_poller_health(), query=query)


def poller_platform_health_observation(
    *,
    snapshot: PollerSnapshot | None,
    source_ref: str,
    received_at: datetime,
) -> PlatformHealthObservation:
    """Project the incumbent poller snapshot without device identities.

    Platform Health owns freshness classification from ``observed_at``. This
    mapper only reports the source state in aggregate: a fresh or stale
    snapshot's device outcome remains separate from local loop liveness.
    """

    if received_at.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    if snapshot is not None and snapshot.observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if snapshot is None:
        return make_platform_health_observation(
            source_ref=source_ref,
            component_code=POLLER_COMPONENT_CODE,
            state=PlatformHealthState.UNKNOWN,
            observed_at=received_at,
            received_at=received_at,
            summary="Bandwidth poller health snapshot unavailable",
            labels=(("probe", "loop_liveness"), ("reason", "unavailable")),
            identity=f"unavailable:{received_at.isoformat()}",
        )
    if snapshot.observed_at > received_at:
        return make_platform_health_observation(
            source_ref=source_ref,
            component_code=POLLER_COMPONENT_CODE,
            state=PlatformHealthState.UNKNOWN,
            observed_at=received_at,
            received_at=received_at,
            summary="Bandwidth poller health snapshot has a future timestamp",
            labels=(("probe", "loop_liveness"), ("reason", "future_timestamp")),
            identity=(
                f"future:{snapshot.poll_count}:{snapshot.observed_at.isoformat()}"
            ),
        )
    if snapshot.devices_total > 0 and snapshot.devices_ok == 0:
        state = PlatformHealthState.UNHEALTHY
    elif snapshot.devices_failing > 0:
        state = PlatformHealthState.DEGRADED
    else:
        state = PlatformHealthState.HEALTHY
    return make_platform_health_observation(
        source_ref=source_ref,
        component_code=POLLER_COMPONENT_CODE,
        state=state,
        observed_at=snapshot.observed_at,
        received_at=received_at,
        summary=(
            "Bandwidth poller cycle observed with "
            f"{snapshot.devices_failing} failing devices"
        ),
        labels=(
            ("devices_failing", str(snapshot.devices_failing)),
            ("devices_total", str(snapshot.devices_total)),
            ("probe", "loop_liveness"),
            ("reason", "snapshot_available"),
        ),
        identity=f"poll:{snapshot.poll_count}:{snapshot.observed_at.isoformat()}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bandwidth poller loop liveness (not readiness)"
    )
    parser.add_argument("--max-age-seconds", type=float, required=True)
    parser.add_argument("--platform-health-source-ref")
    args = parser.parse_args(argv)
    try:
        query = PollerLivenessQuery(
            max_age_seconds=args.max_age_seconds, now=datetime.now(UTC)
        )
    except ValueError:
        parser.error("--max-age-seconds must be in (0, 3600]")
    snapshot = load_poller_health()
    outcome = _classify_poller_snapshot(snapshot=snapshot, query=query)
    if args.platform_health_source_ref is not None:
        try:
            observation = poller_platform_health_observation(
                snapshot=snapshot,
                source_ref=args.platform_health_source_ref,
                received_at=datetime.now(UTC),
            )
        except ValueError:
            parser.error("--platform-health-source-ref is invalid")
        print(json.dumps(observation.to_document()))
    else:
        print(
            json.dumps(
                {
                    "kind": "liveness",
                    "reason": outcome.reason.value,
                    "live": outcome.live,
                    "devices_failing": outcome.devices_failing,
                }
            )
        )
    return 0 if outcome.live else 1


if __name__ == "__main__":
    raise SystemExit(main())
