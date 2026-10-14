"""Dated exceptions (§1.7): activity, lapse, validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

# T14: lapsed from the day after `until`, reported through that day + 7 days.
LAPSE_REPORT_DAYS = 7
MAX_EXCEPTION_DAYS = 180
MIN_REASON_CHARS = 10


class ExceptionKind(StrEnum):
    override = "override"
    mode_override = "mode_override"
    suppress = "suppress"
    annotation = "annotation"


class ExceptionState(StrEnum):
    active = "active"
    pending_approval = "pending_approval"
    revoked = "revoked"
    lapsed = "lapsed"  # past `until`, inside the 7-day reporting window
    expired = "expired"  # past the reporting window


@dataclass(frozen=True)
class ExceptionSpec:
    """A dated, reasoned deviation. Targets one subject or a criteria set."""

    id: str
    module_key: str
    kind: ExceptionKind
    reason: str
    created_on: date
    until: date | None = None
    subject_key: str | None = None
    criteria: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    path: str = ""
    patch: Mapping[str, Any] | None = None
    scope: str | None = None
    mode: str | None = None
    widening: bool = False
    approved: bool = False
    revoked: bool = False
    permanent: bool = False

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.created_on.isoformat(), self.id)


def exception_state(exc: ExceptionSpec, today: date) -> ExceptionState:
    """X1: active iff not revoked, today <= until, and (not widening or approved)."""
    if exc.revoked:
        return ExceptionState.revoked
    if exc.until is not None and today > exc.until:
        if today <= exc.until + timedelta(days=1 + LAPSE_REPORT_DAYS):
            return ExceptionState.lapsed
        return ExceptionState.expired
    if exc.widening and not exc.approved:
        return ExceptionState.pending_approval
    return ExceptionState.active


def exception_applies(
    exc: ExceptionSpec, subject_key: str, dims: Mapping[str, frozenset[str]]
) -> bool:
    if exc.subject_key is not None:
        return exc.subject_key == subject_key
    for dim, wanted in exc.criteria.items():
        if wanted and not (dims.get(dim, frozenset()) & frozenset(wanted)):
            return False
    return bool(exc.criteria)


def basic_exception_issues(exc: ExceptionSpec) -> list[str]:
    """Plug-in independent write checks; the plug-in adds path/derived checks."""
    issues: list[str] = []
    if len(exc.reason.strip()) < MIN_REASON_CHARS:
        issues.append(
            f"exception {exc.id}: reason needs at least {MIN_REASON_CHARS} characters"
        )
    if (exc.subject_key is None) == (not exc.criteria):
        issues.append(
            f"exception {exc.id}: exactly one of subject or criteria is required"
        )
    if exc.permanent:
        issues.append(
            f"exception {exc.id}: permanent is only for adoption-mapping kinds, which do not exist yet"
        )
    elif exc.until is None:
        issues.append(f"exception {exc.id}: until is required")
    elif exc.until > exc.created_on + timedelta(days=MAX_EXCEPTION_DAYS):
        issues.append(
            f"exception {exc.id}: until is more than {MAX_EXCEPTION_DAYS} days out"
        )
    elif exc.until < exc.created_on:
        issues.append(f"exception {exc.id}: until precedes its creation")
    if exc.kind is ExceptionKind.override and exc.patch is None:
        issues.append(f"exception {exc.id}: override needs a patch")
    if exc.kind is ExceptionKind.mode_override and exc.mode not in {
        "enforce",
        "observe",
        "off",
    }:
        issues.append(f"exception {exc.id}: mode_override needs a valid mode")
    if exc.kind is ExceptionKind.suppress and not exc.scope:
        issues.append(f"exception {exc.id}: suppress needs a scope")
    return issues
