"""Shared UI projection contracts (docs/designs/UI_PROJECTION_CONTRACTS.md).

Standard shapes a backend read/context owner returns and a template renders, so
every portal projects KPIs, actions, and cell state the same way and the
presentation layer never re-derives business meaning (the recurring drift the
portal review found). The **List** contract already exists as
``app.services.list_query`` (``ListDefinition`` / ``ListQuery`` — filters, sort,
pagination, counts, and declared capabilities); this module adds the three that
did not exist yet: **State**, **KPI**, and **Action**.

These are transport-neutral: the owner decides value, eligibility, and meaning;
the client owns concrete colours, spacing, and platform-native rendering for
each semantic ``StatusTone`` / ``StatusIcon``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.schemas.status_presentation import StatusIcon, StatusTone

__all__ = ["StateKind", "StateValue", "Kpi", "Action"]


class StateKind(StrEnum):
    """Why a value is or is not present — kept distinct so ``unknown`` never
    renders as zero, ``stale`` is not mistaken for ``unavailable``, and
    ``not_applicable`` is not mistaken for either. Mirrors the state
    distinctions the UI Information & Action standard requires."""

    present = "present"
    unknown = "unknown"  # not loaded / failed to resolve
    stale = "stale"  # a value exists but may be outdated
    unavailable = "unavailable"  # the owning source is down
    not_applicable = "not_applicable"  # the field does not apply to this entity


@dataclass(frozen=True, slots=True)
class StateValue:
    """A cell/metric value that carries its own state and freshness.

    ``value`` is only meaningful when ``kind`` is ``present`` or ``stale``.
    Build one with the classmethods below; a template checks ``is_present``
    before formatting ``value`` and otherwise renders ``placeholder`` — never a
    zero standing in for the unknown.
    """

    kind: StateKind
    value: Any = None
    as_of: datetime | None = None  # freshness for present/stale values

    @classmethod
    def present(cls, value: Any, *, as_of: datetime | None = None) -> StateValue:
        return cls(StateKind.present, value, as_of)

    @classmethod
    def stale(cls, value: Any, *, as_of: datetime | None = None) -> StateValue:
        return cls(StateKind.stale, value, as_of)

    @classmethod
    def unknown(cls) -> StateValue:
        return cls(StateKind.unknown)

    @classmethod
    def unavailable(cls) -> StateValue:
        return cls(StateKind.unavailable)

    @classmethod
    def not_applicable(cls) -> StateValue:
        return cls(StateKind.not_applicable)

    @property
    def is_present(self) -> bool:
        return self.kind in (StateKind.present, StateKind.stale)

    @property
    def is_stale(self) -> bool:
        return self.kind is StateKind.stale

    @property
    def placeholder(self) -> str:
        """Text a template shows when the value is not renderable as itself."""
        return {
            StateKind.present: "",
            StateKind.stale: "",
            StateKind.unknown: "Unknown",
            StateKind.unavailable: "Unavailable",
            StateKind.not_applicable: "—",
        }[self.kind]


@dataclass(frozen=True, slots=True)
class Kpi:
    """A headline number a dashboard or list renders.

    ``value`` carries its own state and freshness, so an unknown KPI never
    becomes zero. ``cohort_url`` is the drill-down to the EXACT filtered cohort
    that produced the number — the projection owner supplies it so a headline
    total and its list can never diverge (the KPI-parity rule). ``tone`` and
    ``icon`` give a non-colour-only semantic signal.
    """

    label: str
    value: StateValue
    cohort_url: str | None = None
    tone: StatusTone = StatusTone.neutral
    icon: StatusIcon | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class Action:
    """One action a screen offers, with eligibility owned by the backend.

    ``allowed`` and ``reason`` come from the owning transition service, never a
    status string re-derived in the template. ``permission`` is the granular
    RBAC key the route enforces (the UI hides what the principal cannot do; the
    route still authorizes). ``preview_url`` + ``danger`` mark
    destructive/financial actions that must show an impact preview and an
    explicit confirmation before running; ``affected`` is a lightweight impact
    count for the same purpose.
    """

    key: str
    label: str
    allowed: bool
    reason: str | None = None
    permission: str | None = None
    preview_url: str | None = None
    affected: int | None = None
    danger: bool = False
