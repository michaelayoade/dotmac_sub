"""The router plug-in: tiers, criteria dimensions, modules, subject builder."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.services.composition.layers import Subject, TierSpec
from app.services.composition.plugin import DomainPlugin
from app.services.router_control.modules import ntp, services_hardening, snmp

LIFECYCLES = ("managed", "observe_only", "unmanaged", "retired")

# §1.4: global -> site -> profile -> group (tags) -> router; exceptions rank above.
ROUTER_TIERS: tuple[TierSpec, ...] = (
    TierSpec("global", 0),
    TierSpec("site", 1, allowed_dims=("sites",), require_any=("sites",)),
    TierSpec(
        "profile",
        2,
        weight_band=(100, 900),
        allowed_dims=("profiles",),
        require_any=("profiles",),
        kind_bands={"role": (100, 400), "feature": (500, 900)},
    ),
    TierSpec(
        "group",
        3,
        allowed_dims=("tags", "profiles", "hardware_classes", "sites"),
        require_any=("tags", "profiles", "hardware_classes"),
    ),
    TierSpec(
        "router",
        4,
        weight_band=(0, 0),
        allowed_dims=("router_ids",),
        require_any=("router_ids",),
    ),
)

ROUTER_PLUGIN = DomainPlugin(
    "router",
    ROUTER_TIERS,
    {
        snmp.MODULE.key: snmp.MODULE,
        ntp.MODULE.key: ntp.MODULE,
        services_hardening.MODULE.key: services_hardening.MODULE,
    },
)


@dataclass(frozen=True)
class RouterBinding:
    """Human-declared subject facts for one router (§1.1 binding + assignments).

    ``tags`` are policy tags, never the inventory ``routers.tags`` column.
    """

    router_id: str
    name: str
    site: str | None
    profiles: tuple[str, ...]
    tags: tuple[str, ...] = ()
    hardware_class: str | None = None
    lifecycle: str = "managed"
    loopback_v4: str | None = None
    loopback_v6: str | None = None


def build_subject(binding: RouterBinding) -> Subject:
    loopbacks = tuple(a for a in (binding.loopback_v4, binding.loopback_v6) if a)
    return Subject(
        key=binding.router_id,
        dims={
            "sites": frozenset({binding.site} if binding.site else ()),
            "profiles": frozenset(binding.profiles),
            "tags": frozenset(binding.tags),
            "hardware_classes": frozenset(
                {binding.hardware_class} if binding.hardware_class else ()
            ),
            "router_ids": frozenset({binding.router_id}),
        },
        facts={
            "loopback_v4": binding.loopback_v4,
            "loopbacks": loopbacks,
        },
    )


def binding_issues(bindings: Iterable[RouterBinding]) -> list[str]:
    issues: list[str] = []
    for binding in bindings:
        if binding.lifecycle not in LIFECYCLES:
            issues.append(
                f"{binding.name}: lifecycle must be one of {list(LIFECYCLES)}"
            )
        if binding.lifecycle == "managed" and not binding.profiles:
            issues.append(
                f"{binding.name}: a managed router needs at least one profile"
            )
    return issues
