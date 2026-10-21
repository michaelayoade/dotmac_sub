"""Derived fields: pure functions of other modules' effective values (§1.8)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class DerivationCycleError(ValueError):
    """The derived-field dependency graph has a cycle."""


class _NotDerived:
    def __repr__(self) -> str:
        return "NOT_DERIVED"


NOT_DERIVED: Any = _NotDerived()


@dataclass(frozen=True)
class Ref:
    """A rule input: ``module`` + JSON pointer, or a subject fact (module None)."""

    module: str | None
    path: str

    @property
    def label(self) -> str:
        return (
            f"fact:{self.path}" if self.module is None else f"{self.module}:{self.path}"
        )


@dataclass(frozen=True)
class DerivedRule:
    name: str
    module: str
    target: str  # JSON pointer inside the module's params
    reads: tuple[Ref, ...]
    fn: Callable[[Mapping[str, Any]], Any]  # label -> value; return NOT_DERIVED to skip


def _overlaps(a: str, b: str) -> bool:
    return (
        a == b or a.startswith(b + "/") or b.startswith(a + "/") or a == "" or b == ""
    )


def order_rules(rules: Sequence[DerivedRule]) -> list[DerivedRule]:
    """Topological order (stable on rule name); a cycle raises."""
    by_name = {rule.name: rule for rule in rules}
    if len(by_name) != len(rules):
        raise DerivationCycleError("duplicate derived rule name")
    deps: dict[str, set[str]] = {rule.name: set() for rule in rules}
    for rule in rules:
        for ref in rule.reads:
            if ref.module is None:
                continue
            for other in rules:
                if other.module == ref.module and _overlaps(ref.path, other.target):
                    if other is rule:
                        raise DerivationCycleError(
                            f"rule {rule.name} reads its own target"
                        )
                    deps[rule.name].add(other.name)
    ordered: list[DerivedRule] = []
    state: dict[str, int] = {}

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise DerivationCycleError(
                "derived-field cycle: " + " -> ".join(stack + (name,))
            )
        state[name] = 1
        for dep in sorted(deps[name]):
            visit(dep, stack + (name,))
        state[name] = 2
        ordered.append(by_name[name])

    for name in sorted(by_name):
        visit(name, ())
    return ordered
