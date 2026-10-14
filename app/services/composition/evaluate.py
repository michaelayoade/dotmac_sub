"""Drift evaluation of one composed module against an observation (§1.10)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .compose import ModuleResult
from .drift import (
    NON_DRIFT,
    DriftItem,
    ModuleEvaluation,
    ModuleStatus,
    NotEvaluated,
    Unknown,
)
from .plugin import Module


def _matches(scope: str, item: DriftItem) -> bool:
    return scope in {"*", item.path, item.element or "\0"} or (
        item.element is not None and scope == f"{item.path}/{item.element}"
    )


def evaluate_module(
    module: Module,
    result: ModuleResult,
    observation: Any,
    facts: Mapping[str, Any],
) -> ModuleEvaluation:
    """Unobserved is unknown, never absent: no snapshot never yields ``missing``."""
    if result.mode == "off":
        return ModuleEvaluation(module.key, result.mode, ModuleStatus.off)
    if not result.ok:
        return ModuleEvaluation(
            module.key,
            result.mode,
            ModuleStatus.not_evaluated,
            NotEvaluated.invalid_desired,
            detail="; ".join(f"{r.code} {r.path}".strip() for r in result.reasons),
        )
    if isinstance(observation, Unknown):
        return ModuleEvaluation(
            module.key,
            result.mode,
            ModuleStatus.not_evaluated,
            observation.reason,
            detail=observation.detail,
        )
    observed = module.extract_observed(observation)
    if isinstance(observed, Unknown):
        return ModuleEvaluation(
            module.key,
            result.mode,
            ModuleStatus.not_evaluated,
            observed.reason,
            detail=observed.detail,
        )
    desired = module.params_schema.model_validate(result.params)
    items: list[DriftItem] = []
    for item in module.compare(desired, observed, facts):
        origin = item.origin or result.origin_for(item.path)
        excepted = any(_matches(s.scope, item) for s in result.suppressions)
        items.append(replace(item, origin=origin, excepted=excepted))
    drift = [i for i in items if i.status not in NON_DRIFT]
    if result.mode == "observe":
        status = ModuleStatus.observe_only
    elif not drift:
        status = ModuleStatus.conform
    elif all(i.excepted for i in drift):
        status = ModuleStatus.excepted
    else:
        status = ModuleStatus.drift
    return ModuleEvaluation(
        module.key,
        result.mode,
        status,
        items=tuple(items),
        proposals=module.propose(desired, observed),
    )
