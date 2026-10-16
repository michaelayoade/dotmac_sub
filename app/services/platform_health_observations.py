"""Bounded Platform Health observation documents produced by local probes.

This adapter mirrors the field and vocabulary contract of Starter's
``HealthObservationInput`` without importing Platform Health persistence or
transport. An authenticated caller supplies ``source_ref``; this module only
validates and serializes a bounded observation for a future authenticated
Integrator transport. It does not sign evidence or make deployment decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

_TOKEN = re.compile(r"^[!-~]+$")


class PlatformHealthState(StrEnum):
    """Starter Platform Health state vocabulary."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


def _bounded_token(value: str, *, maximum: int, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or _TOKEN.fullmatch(value) is None
    ):
        raise ValueError(f"invalid {name}")
    return value


def _bounded_text(value: str, *, maximum: int, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class PlatformHealthObservation:
    """One bounded, provider-neutral health observation at the source edge."""

    source_ref: str
    observation_key: str
    component_code: str
    state: PlatformHealthState
    observed_at: datetime
    received_at: datetime
    summary: str
    labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _bounded_token(self.source_ref, maximum=200, name="source_ref")
        _bounded_token(self.observation_key, maximum=200, name="observation_key")
        _bounded_token(self.component_code, maximum=120, name="component_code")
        if not isinstance(self.state, PlatformHealthState):
            raise ValueError("invalid state")
        observed_at = _aware(self.observed_at, "observed_at")
        received_at = _aware(self.received_at, "received_at")
        if observed_at > received_at:
            raise ValueError("observed_at cannot be later than received_at")
        _bounded_text(self.summary, maximum=240, name="summary")
        if not isinstance(self.labels, tuple) or len(self.labels) > 20:
            raise ValueError("invalid labels")
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for pair in self.labels:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("invalid labels")
            key = _bounded_text(pair[0], maximum=80, name="label key")
            value = _bounded_text(pair[1], maximum=160, name="label value")
            if key in seen:
                raise ValueError("duplicate label key")
            seen.add(key)
            normalized.append((key, value))
        object.__setattr__(self, "labels", tuple(sorted(normalized)))

    def to_document(self) -> dict[str, object]:
        """Serialize with Starter HealthObservationInput field names."""

        return {
            "source_ref": self.source_ref,
            "observation_key": self.observation_key,
            "component_code": self.component_code,
            "state": self.state.value,
            "observed_at": self.observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "summary": self.summary,
            "labels": dict(self.labels),
        }


def make_platform_health_observation(
    *,
    source_ref: str,
    component_code: str,
    state: PlatformHealthState,
    observed_at: datetime,
    received_at: datetime,
    summary: str,
    labels: tuple[tuple[str, str], ...],
    identity: str,
) -> PlatformHealthObservation:
    """Build an observation with a deterministic replay-stable key.

    ``identity`` is source-local provenance used only to derive the key. It is
    never serialized, so process generations and monotonic readings do not
    become health labels or evidence fields.
    """

    _bounded_token(identity, maximum=200, name="identity")
    candidate = PlatformHealthObservation(
        source_ref=source_ref,
        observation_key="pending",
        component_code=component_code,
        state=state,
        observed_at=observed_at,
        received_at=received_at,
        summary=summary,
        labels=labels,
    )
    payload = {
        **candidate.to_document(),
        "identity": identity,
    }
    payload.pop("observation_key")
    observation_key = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PlatformHealthObservation(
        source_ref=candidate.source_ref,
        observation_key=observation_key,
        component_code=candidate.component_code,
        state=candidate.state,
        observed_at=candidate.observed_at,
        received_at=candidate.received_at,
        summary=candidate.summary,
        labels=candidate.labels,
    )
