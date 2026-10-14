"""Drift vocabulary (§1.10): items, module statuses, unknown observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .merge import Origin


class DriftStatus(StrEnum):
    ok = "ok"
    missing = "missing"
    drifted = "drifted"
    unexpected = "unexpected"
    disabled = "disabled"
    duplicated = "duplicated"
    misplaced = "misplaced"
    unverifiable_secret = "unverifiable_secret"
    not_applicable = "not_applicable"


NON_DRIFT = frozenset({DriftStatus.ok, DriftStatus.not_applicable})


class ModuleStatus(StrEnum):
    conform = "conform"
    drift = "drift"
    excepted = "excepted"
    observe_only = "observe_only"
    not_evaluated = "not_evaluated"
    off = "off"


class NotEvaluated(StrEnum):
    no_snapshot = "no_snapshot"
    stale = "stale"
    unparseable = "unparseable"
    invalid_desired = "invalid_desired"
    blocked_by_version = "blocked_by_version"


@dataclass(frozen=True)
class Unknown:
    """Unobserved is unknown, never absent (Starter ADR-0032)."""

    reason: NotEvaluated
    detail: str | None = None


@dataclass(frozen=True)
class DriftItem:
    module: str
    path: str
    status: DriftStatus
    desired: Any = None
    observed: Any = None
    element: str | None = None
    severity: str = "low"
    origin: Origin | None = None
    excepted: bool = False
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "path": self.path,
            "element": self.element,
            "status": self.status.value,
            "severity": self.severity,
            "desired": self.desired,
            "observed": self.observed,
            "origin": self.origin.as_dict() if self.origin else None,
            "excepted": self.excepted,
            "note": self.note,
        }


@dataclass(frozen=True)
class Proposal:
    """A discovered fact offered for adoption. Never applied automatically."""

    module: str
    path: str
    op: str  # add | remove | set
    value: Any
    widening: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "path": self.path,
            "op": self.op,
            "value": self.value,
            "widening": self.widening,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class Suppression:
    exception_id: str
    module: str
    scope: str
    reason: str
    until: str | None


@dataclass(frozen=True)
class ModuleEvaluation:
    module: str
    mode: str
    status: ModuleStatus
    reason: NotEvaluated | None = None
    items: tuple[DriftItem, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    detail: str | None = None

    @property
    def drifted(self) -> bool:
        return self.status is ModuleStatus.drift

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "mode": self.mode,
            "status": self.status.value,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "items": [item.as_dict() for item in self.items],
            "proposals": [p.as_dict() for p in self.proposals],
        }
