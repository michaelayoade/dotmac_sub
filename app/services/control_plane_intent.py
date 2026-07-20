"""Shared semantic contract for network control-plane intent delivery.

Vendor adapters keep their native persistence states. This module gives those
states one lifecycle for orchestration, reporting, and safety checks without
making one vendor model authoritative for another.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class ControlPlanePhase(str, enum.Enum):
    desired = "desired"
    planned = "planned"
    queued = "queued"
    applying = "applying"
    readback_pending = "readback_pending"
    verified = "verified"
    drifted = "drifted"
    failed = "failed"


class ControlPlaneContractError(ValueError):
    """Base error for invalid lifecycle and revision operations."""


class ControlPlaneTransitionError(ControlPlaneContractError):
    pass


class ControlPlaneHeadConflict(ControlPlaneContractError):
    pass


@dataclass(frozen=True)
class ControlPlaneTarget:
    """Canonical identity for one desired-state revision."""

    provider: str
    target_type: str
    target_id: str
    desired_revision: int

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("target_type", self.target_type),
            ("target_id", self.target_id),
        ):
            cleaned = str(value).strip()
            if not cleaned:
                raise ControlPlaneContractError(f"{name} is required")
            if name in {"provider", "target_type"}:
                cleaned = cleaned.lower()
            object.__setattr__(self, name, cleaned)
        if self.desired_revision < 1:
            raise ControlPlaneContractError("desired_revision must be positive")

    @property
    def correlation_key(self) -> str:
        return (
            f"{self.provider}:{self.target_type}:{self.target_id}:"
            f"revision:{self.desired_revision}"
        )

    def as_payload(self) -> dict[str, str | int]:
        return {
            "provider": self.provider,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "desired_revision": self.desired_revision,
        }


_ALLOWED_TRANSITIONS: dict[ControlPlanePhase, frozenset[ControlPlanePhase]] = {
    ControlPlanePhase.desired: frozenset(
        {
            ControlPlanePhase.planned,
            ControlPlanePhase.queued,
            ControlPlanePhase.applying,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.planned: frozenset(
        {ControlPlanePhase.queued, ControlPlanePhase.failed}
    ),
    ControlPlanePhase.queued: frozenset(
        {
            ControlPlanePhase.applying,
            ControlPlanePhase.readback_pending,
            ControlPlanePhase.verified,
            ControlPlanePhase.drifted,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.applying: frozenset(
        {
            ControlPlanePhase.readback_pending,
            ControlPlanePhase.verified,
            ControlPlanePhase.drifted,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.readback_pending: frozenset(
        {
            ControlPlanePhase.applying,
            ControlPlanePhase.verified,
            ControlPlanePhase.drifted,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.verified: frozenset(
        {ControlPlanePhase.desired, ControlPlanePhase.drifted}
    ),
    ControlPlanePhase.drifted: frozenset(
        {
            ControlPlanePhase.desired,
            ControlPlanePhase.planned,
            ControlPlanePhase.queued,
            ControlPlanePhase.applying,
            ControlPlanePhase.readback_pending,
            ControlPlanePhase.verified,
            ControlPlanePhase.failed,
        }
    ),
    ControlPlanePhase.failed: frozenset(
        {
            ControlPlanePhase.desired,
            ControlPlanePhase.planned,
            ControlPlanePhase.queued,
            ControlPlanePhase.applying,
            ControlPlanePhase.readback_pending,
        }
    ),
}


def assert_phase_transition(
    current: ControlPlanePhase, destination: ControlPlanePhase
) -> None:
    """Reject an impossible semantic transition; idempotent writes are allowed."""
    if current == destination:
        return
    if destination not in _ALLOWED_TRANSITIONS[current]:
        raise ControlPlaneTransitionError(
            f"Cannot transition control-plane intent from '{current.value}' "
            f"to '{destination.value}'"
        )


def assert_intent_head(*, expected_revision: int, current_revision: int) -> None:
    """Prevent a queued operation from writing a superseded intent revision."""
    if expected_revision < 1:
        raise ControlPlaneContractError("expected_revision must be positive")
    if expected_revision != current_revision:
        raise ControlPlaneHeadConflict(
            f"Intent revision {expected_revision} is stale; current revision is "
            f"{current_revision}"
        )


def _status_value(status: Any) -> str:
    value = getattr(status, "value", status)
    return str(value).strip().lower()


def phase_for_network_operation(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "waiting": ControlPlanePhase.readback_pending,
            "succeeded": ControlPlanePhase.verified,
            "warning": ControlPlanePhase.drifted,
            "failed": ControlPlanePhase.failed,
            "canceled": ControlPlanePhase.failed,
        },
        source="NetworkOperation",
    )


def phase_for_uisp_intent(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "staged": ControlPlanePhase.desired,
            "applying": ControlPlanePhase.applying,
            "pending_readback": ControlPlanePhase.readback_pending,
            "pending_observation": ControlPlanePhase.readback_pending,
            "verified": ControlPlanePhase.verified,
            "drifted": ControlPlanePhase.drifted,
            "manual_required": ControlPlanePhase.drifted,
            "failed": ControlPlanePhase.failed,
            "decommissioned": ControlPlanePhase.verified,
        },
        source="UISP intent",
    )


def phase_for_huawei_sync(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "synced": ControlPlanePhase.verified,
            "reconciling": ControlPlanePhase.applying,
            "out_of_sync": ControlPlanePhase.drifted,
        },
        source="Huawei reconcile",
    )


def phase_for_router_push(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "pending_readback": ControlPlanePhase.readback_pending,
            "completed": ControlPlanePhase.verified,
            "partial_failure": ControlPlanePhase.drifted,
            "failed": ControlPlanePhase.failed,
            "rolled_back": ControlPlanePhase.failed,
        },
        source="RouterOS push",
    )


def phase_for_router_push_result(status: Any) -> ControlPlanePhase:
    return _project(
        status,
        {
            "pending": ControlPlanePhase.queued,
            "running": ControlPlanePhase.applying,
            "pending_readback": ControlPlanePhase.readback_pending,
            "success": ControlPlanePhase.verified,
            "failed": ControlPlanePhase.failed,
            "skipped": ControlPlanePhase.failed,
        },
        source="RouterOS push result",
    )


def _project(
    status: Any,
    mapping: dict[str, ControlPlanePhase],
    *,
    source: str,
) -> ControlPlanePhase:
    value = _status_value(status)
    try:
        return mapping[value]
    except KeyError as exc:
        raise ControlPlaneContractError(f"Unknown {source} status '{value}'") from exc
