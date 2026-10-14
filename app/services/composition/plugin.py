"""Module protocol and domain plug-in registry (§1A.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from .derive import DerivationCycleError, DerivedRule, order_rules
from .drift import DriftItem, Proposal
from .fields import (
    FieldSpec,
    Kind,
    SpecIndex,
    parse_spec_pointer,
    schema_problems,
    walk_schema,
)
from .layers import TierSpec
from .merge import Origin

MODES = ("enforce", "observe", "off")


class PluginError(ValueError):
    """A plug-in declaration is inconsistent (fails closed at load time)."""


@dataclass(frozen=True)
class SurfaceClaim:
    """A device surface a module claims, with an optional element predicate."""

    path: str
    predicate: str = "*"


@dataclass(frozen=True)
class Invalid:
    code: str  # stale_layer | missing_required | schema | self_reference | double_claim | ...
    path: str = ""
    detail: str = ""
    origin: Origin | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "detail": self.detail,
            "origin": self.origin.as_dict() if self.origin else None,
        }


class Module(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def params_schema(self) -> type[BaseModel]: ...

    @property
    def field_specs(self) -> Mapping[str, FieldSpec]: ...

    @property
    def surfaces(self) -> tuple[SurfaceClaim, ...]: ...

    @property
    def health_checks(self) -> tuple[str, ...]: ...

    @property
    def derives(self) -> tuple[DerivedRule, ...]: ...

    @property
    def mode_ordered(self) -> bool: ...

    def extract_observed(self, observation: Any) -> Any: ...

    def compare(
        self, desired: Any, observed: Any, facts: Mapping[str, Any]
    ) -> tuple[DriftItem, ...]: ...

    def propose(self, desired: Any, observed: Any) -> tuple[Proposal, ...]: ...

    def invariants(
        self, params: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> tuple[Invalid, ...]: ...

    def claims(self, params: Mapping[str, Any]) -> frozenset[str]: ...


class ModuleBase:
    """Defaults for the optional parts of the protocol."""

    surfaces: tuple[SurfaceClaim, ...] = ()
    health_checks: tuple[str, ...] = ()
    derives: tuple[DerivedRule, ...] = ()
    mode_ordered: bool = False

    def propose(self, desired: Any, observed: Any) -> tuple[Proposal, ...]:
        return ()

    def invariants(
        self, params: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> tuple[Invalid, ...]:
        return ()

    def claims(self, params: Mapping[str, Any]) -> frozenset[str]:
        return frozenset()


@dataclass(frozen=True)
class DomainPlugin:
    name: str
    tiers: tuple[TierSpec, ...]
    modules: Mapping[str, Module]
    _spec_cache: dict[str, SpecIndex] = field(
        default_factory=dict, compare=False, repr=False
    )
    _ann_cache: dict[str, dict[tuple[str, ...], Any]] = field(
        default_factory=dict, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        problems = plugin_problems(self)
        if problems:
            raise PluginError("; ".join(problems))

    def tier(self, name: str) -> TierSpec:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise PluginError(f"unknown tier {name!r}")

    def specs(self, module_key: str) -> SpecIndex:
        cached = self._spec_cache.get(module_key)
        if cached is None:
            cached = SpecIndex(self.modules[module_key].field_specs)
            self._spec_cache[module_key] = cached
        return cached

    def leaf_annotations(self, module_key: str) -> dict[tuple[str, ...], Any]:
        if module_key not in self._ann_cache:
            self._ann_cache[module_key] = {
                leaf.path: (leaf.element if leaf.kind is Kind.list else leaf.annotation)
                for leaf in walk_schema(self.modules[module_key].params_schema)
            }
        return self._ann_cache[module_key]

    def all_rules(self) -> list[DerivedRule]:
        rules: list[DerivedRule] = []
        for key in sorted(self.modules):
            rules.extend(self.modules[key].derives)
        return order_rules(rules)


def plugin_problems(plugin: DomainPlugin) -> list[str]:
    """Registry-load checks: tiers, field specs (guard 10), derivation DAG."""
    problems: list[str] = []
    names = [t.name for t in plugin.tiers]
    if len(set(names)) != len(names):
        problems.append("duplicate tier names")
    ranks = [t.rank for t in plugin.tiers]
    if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):
        problems.append("tiers must be listed by strictly increasing rank")
    rules: list[DerivedRule] = []
    for key, module in sorted(plugin.modules.items()):
        if module.key != key:
            problems.append(f"module registered as {key!r} declares key {module.key!r}")
        for problem in schema_problems(module.params_schema, module.field_specs):
            problems.append(f"{key}: {problem}")
        index = SpecIndex(module.field_specs)
        for rule in module.derives:
            rules.append(rule)
            if rule.module != key:
                problems.append(
                    f"rule {rule.name}: belongs to {rule.module!r}, not {key!r}"
                )
            spec = index.get_exact(parse_spec_pointer(rule.target))
            if spec is None or not spec.derived or spec.kind is Kind.object:
                problems.append(
                    f"rule {rule.name}: target {rule.target} is not a derived field"
                )
        for pointer_text, spec in module.field_specs.items():
            if spec.derived and not any(
                r.target == pointer_text for r in module.derives
            ):
                problems.append(f"{key}: derived field {pointer_text} has no rule")
    for rule in rules:
        for ref in rule.reads:
            if ref.module is not None and ref.module not in plugin.modules:
                problems.append(
                    f"rule {rule.name}: reads unknown module {ref.module!r}"
                )
    try:
        order_rules(rules)
    except DerivationCycleError as exc:
        problems.append(str(exc))
    return problems
