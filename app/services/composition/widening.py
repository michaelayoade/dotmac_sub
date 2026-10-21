"""Widening classification (§1.9): does an edit loosen a security posture?"""

from __future__ import annotations

import copy
import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .document import pointer
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


def _fresh_before(spec: FieldSpec) -> Any:
    """What a field granted before its (new) container existed: nothing."""
    if spec.access is AccessKind.secure_bool:
        return spec.secure
    if spec.access is AccessKind.ordered and spec.ranking:
        return spec.ranking[0]
    return None


def params_widening(
    specs: SpecIndex,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[WideningItem]:
    """Widened fields between two effective parameter documents.

    Walks objects, maps (by member key) and keyed lists (by element key), so
    per-element ACLs are covered. An unset field is compared as its schema
    default (else "nothing granted"). A member new in ``after`` is compared
    against "nothing granted". A member that disappears is treated as granting
    nothing: removing an allow-list or secure-boolean member narrows, removing
    a deny-list member loses its protection and is a widening (§1.9).
    """
    items: list[WideningItem] = []

    def walk(
        old: Any, new: Any, path: Path, *, fresh: bool, gone: bool = False
    ) -> None:
        old_map = old if isinstance(old, Mapping) else {}
        new_map = new if isinstance(new, Mapping) else {}
        for name in sorted(set(old_map) | set(new_map)):
            child = path + (name,)
            spec = specs.lookup(child)
            if spec is None:
                continue
            old_v, new_v = old_map.get(name), new_map.get(name)
            if old_v is None and new_v is None:
                continue
            is_fresh = fresh
            is_gone = gone
            if spec.kind in {Kind.object, Kind.map}:
                if spec.kind is Kind.map:
                    for member in sorted(
                        set(old_v if isinstance(old_v, Mapping) else {})
                        | set(new_v if isinstance(new_v, Mapping) else {})
                    ):
                        member_old = (old_v or {}).get(member)
                        member_new = (new_v or {}).get(member)
                        walk(
                            member_old,
                            member_new,
                            child + (str(member),),
                            fresh=is_fresh or member_old is None,
                            gone=is_gone or member_new is None,
                        )
                else:
                    walk(
                        old_v,
                        new_v,
                        child,
                        fresh=is_fresh or old_v is None,
                        gone=is_gone,  # an absent plain object is unmanaged, not secure
                    )
            elif spec.kind is Kind.list and spec.strategy is Strategy.keyed:
                assert spec.key is not None
                olds = {str(e[spec.key]): e for e in old_v or []}
                news = {str(e[spec.key]): e for e in new_v or []}
                for element in sorted(set(olds) | set(news)):
                    walk(
                        olds.get(element),
                        news.get(element),
                        child + (element,),
                        fresh=is_fresh or element not in olds,
                        gone=is_gone or element not in news,
                    )
            else:
                if old_v is None:
                    # an unset field holds its schema default (merged values are
                    # compared before defaults are filled), else grants nothing
                    old_v = (
                        copy.deepcopy(spec.default)
                        if spec.has_value_default
                        else _fresh_before(spec)
                    )
                if gone and new_v is None:
                    new_v = _fresh_before(spec)  # a removed member grants nothing
                if widening(spec, old_v, new_v):
                    items.append(WideningItem(pointer(child), old_v, new_v))

    walk(before, after, (), fresh=False)
    return items
