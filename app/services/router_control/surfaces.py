"""RouterOS surfaces claimed by PR1 modules and the claim registry check."""

from __future__ import annotations

from app.services.composition.plugin import DomainPlugin

# Surfaces owned by modules that do not exist yet; the later unclaimed-surface
# report (PR4c) will name them. Listed so a new module cannot silently take one.
KNOWN_UNCLAIMED: frozenset[str] = frozenset(
    {
        "/ip service:ssh|winbox|api|www-ssl",
        "/ip firewall filter",
        "/ipv6 firewall filter",
        "/routing bgp connection",
        "/routing ospf",
        "/interface eoip",
        "/radius",
        "/user",
    }
)


def claim_problems(plugin: DomainPlugin) -> list[str]:
    """No two modules claim the same (path, predicate)."""
    owners: dict[tuple[str, str], str] = {}
    problems: list[str] = []
    for key in sorted(plugin.modules):
        for claim in plugin.modules[key].surfaces:
            slot = (claim.path, claim.predicate)
            if slot in owners:
                problems.append(
                    f"{claim.path} [{claim.predicate}] claimed by {owners[slot]} and {key}"
                )
            owners[slot] = key
    return problems
