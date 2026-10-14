"""Tiers, layers, subjects, applicability (NetBox matching) and ordering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class LayerError(ValueError):
    """A layer, revision or document is invalid."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True)
class TierSpec:
    """One tier of a plug-in.

    ``allowed_dims`` are the criteria dimensions a layer of this tier may use.
    ``require_any`` demands at least one non-empty dimension among them; an empty
    ``allowed_dims`` means the tier takes no criteria and always applies.
    ``kind_bands`` maps a layer ``kind`` to its inclusive weight band; otherwise
    ``weight_band`` applies.
    """

    name: str
    rank: int
    weight_band: tuple[int, int] = (0, 999)
    allowed_dims: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    kind_bands: Mapping[str, tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Layer:
    id: str
    tier: str
    name: str
    weight: int
    criteria: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    active: bool = True
    kind: str | None = None


@dataclass(frozen=True)
class LayerRevision:
    layer: Layer
    revision: int
    document: Mapping[str, Any]
    activated_at: str | None = None


@dataclass(frozen=True)
class Subject:
    """What layers are matched against. Human-declared values only (§1.4)."""

    key: str
    dims: Mapping[str, frozenset[str]] = field(default_factory=dict)
    facts: Mapping[str, Any] = field(default_factory=dict)


def validate_layer_meta(tier: TierSpec, layer: Layer) -> list[str]:
    issues: list[str] = []
    band = tier.weight_band
    if tier.kind_bands:
        if layer.kind is None or layer.kind not in tier.kind_bands:
            issues.append(
                f"layer {layer.name}: kind must be one of {sorted(tier.kind_bands)}"
            )
        else:
            band = tier.kind_bands[layer.kind]
    if not band[0] <= layer.weight <= band[1]:
        issues.append(f"layer {layer.name}: weight {layer.weight} outside {band}")
    used = {dim for dim, values in layer.criteria.items() if values}
    stray = sorted(set(layer.criteria) - set(tier.allowed_dims))
    if stray:
        issues.append(
            f"layer {layer.name}: criteria {stray} not allowed at tier {tier.name}"
        )
    if not tier.allowed_dims and used:
        issues.append(f"layer {layer.name}: tier {tier.name} takes no criteria")
    if tier.require_any and not (used & set(tier.require_any)):
        issues.append(
            f"layer {layer.name}: tier {tier.name} needs one of {list(tier.require_any)}"
        )
    return issues


def layer_applies(tier: TierSpec, layer: Layer, subject: Subject) -> bool:
    """NetBox matching: AND across dimensions, OR within one, empty = open."""
    if not layer.active:
        return False
    for dim, wanted in layer.criteria.items():
        if not wanted:
            continue
        if not (subject.dims.get(dim, frozenset()) & frozenset(wanted)):
            return False
    return True


def sort_key(tier: TierSpec, layer: Layer) -> tuple[int, int, str, str]:
    return (tier.rank, layer.weight, layer.name, layer.id)
