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


def _service_names(entry_prefix: str) -> frozenset[str]:
    names: set[str] = set()
    for entry in KNOWN_UNCLAIMED:
        if entry.startswith(entry_prefix):
            names.update(entry.split(":", 1)[1].split("|"))
    return frozenset(names)


# `/ip service` entries that belong to the later mgmt-access module (no PR1
# module may require them disabled).
UNCLAIMED_SERVICES: frozenset[str] = _service_names("/ip service:")
# Services that carry router management (REST over www-ssl, SSH, the RouterOS
# API). PR1 has no per-router transport fact, so the lockout guard treats all
# of them as the management transport.
MANAGEMENT_TRANSPORT_SERVICES: frozenset[str] = frozenset({"ssh", "api", "www-ssl"})


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
