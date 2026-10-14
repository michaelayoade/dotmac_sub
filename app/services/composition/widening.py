"""Widening classification (§1.9): does an edit loosen a security posture?"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .fields import AccessKind, FieldSpec, Kind, Path, SpecIndex, Strategy

MODE_RANKING = ("enforce", "observe", "off")  # most secure first


def _network(value: Any) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return None


def _covers(outer: Any, inner: Any) -> bool:
    a, b = _network(outer), _network(inner)
    if a is not None and b is not None:
        return a.version == b.version and b.subnet_of(a)  # type: ignore[arg-type]
    return str(outer) == str(inner)


def _covered_by_any(element: Any, pool: Iterable[Any]) -> bool:
    return any(_covers(candidate, element) for candidate in pool)


def widening(spec: FieldSpec, before: Any, after: Any) -> bool:
    """True when ``after`` is a widening of ``before`` for this field."""
    if spec.access is AccessKind.neutral:
        return False
    if spec.access is AccessKind.allow_list:
        old, new = list(before or []), list(after or [])
        return any(not _covered_by_any(item, old) for item in new)
    if spec.access is AccessKind.deny_list:
        old, new = list(before or []), list(after or [])
        return any(not _covered_by_any(item, new) for item in old)
    if spec.access is AccessKind.secure_bool:
        return before == spec.secure and after != spec.secure
    if spec.access is AccessKind.ordered:
        return spec.security_rank(after) > spec.security_rank(before)
    return False


def mode_widening(before: str | None, after: str | None) -> bool:
    """Relaxing a security module's mode (enforce > observe > off)."""
    if before is None or after is None:
        return False

    def rank(mode: str) -> int:
        return MODE_RANKING.index(mode) if mode in MODE_RANKING else len(MODE_RANKING)

    return rank(after) > rank(before)


@dataclass(frozen=True)
class WideningItem:
    path: str
    before: Any
    after: Any


def _get(values: Mapping[str, Any], path: Path) -> Any:
    cursor: Any = values
    for segment in path:
        if not isinstance(cursor, Mapping) or segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


def params_widening(
    specs: SpecIndex,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[WideningItem]:
    """Widened fields between two effective parameter documents."""
    items: list[WideningItem] = []
    for path, spec in specs.items():
        if "*" in path or spec.kind not in {Kind.scalar, Kind.list}:
            continue
        if spec.kind is Kind.list and spec.strategy is Strategy.keyed:
            continue
        old, new = _get(before, path), _get(after, path)
        if old is None and new is None:
            continue
        if widening(spec, old, new):
            items.append(WideningItem("/" + "/".join(path), old, new))
    return items
