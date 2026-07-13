"""A declared owner must actually be reachable.

`tests/test_sot_relationships.py` asserts almost nothing about reality: three of
its four tests restate the registry's own literals back to it, and the fourth only
checks that each module IMPORTS — which every dead module passes.

So the registry could, and did, declare owners that nothing in `app/` ever calls.
The map claims a service owns a decision while the decision is made somewhere else
entirely, and the map is the thing people read to find the owner.

This asserts the weakest useful property: every declared owner has at least one
real caller. It is not "the module owns what it claims" — that needs a human. It
is "this module is not a corpse".
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
# Operator tooling is a real caller. Two declared owners are reached ONLY from
# scripts/one_off — scanning app/ alone would call live code dead, and deleting
# it would break the tools operators actually run.
SCRIPT_DIR = PROJECT_ROOT / "scripts"

# Owners with no importer in app/ TODAY. Each is a decision that is declared to
# have an owner and does not have one — the module is unreachable, so whatever it
# claims to own is in practice owned by nobody, or by something else.
#
# This list may only SHRINK: either wire the owner up, or strike it from the
# registry. It exists so the property can be enforced for everything else instead
# of waiting for a cleanup that may never come.
KNOWN_DEAD_OWNERS: set[str] = set()


def _imported_modules(path: Path) -> set[str]:
    """Every module this file imports, by dotted name."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken file is another test's job
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def _import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for root in (APP_DIR, SCRIPT_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(PROJECT_ROOT)
            dotted = ".".join(rel.with_suffix("").parts)
            graph[dotted] = _imported_modules(path)
    return graph


def _has_real_caller(module: str, graph: dict[str, set[str]]) -> bool:
    """Is this module IMPORTED by anything?

    A package ``__init__`` that re-exports it DOES count. Several owners are
    legitimately reached through their package facade — `financial.ledger` is used
    as ``billing_service.ledger_entries``, not by importing the module directly —
    and calling that dead would be wrong.

    What does not count is the registry naming it: that is the claim under test,
    not evidence for it. A module listed only in an ``__all__`` string is likewise
    not imported, and stays dead.
    """
    for importer, imports in graph.items():
        if importer == module:
            continue
        if importer.endswith("sot_relationships"):
            continue
        if module in imports:
            return True
    return False


def test_every_declared_owner_has_a_real_caller() -> None:
    graph = _import_graph()

    dead: list[str] = []
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if service.module in KNOWN_DEAD_OWNERS:
                continue
            if not _has_real_caller(service.module, graph):
                dead.append(f"{service.name} -> {service.module}")

    assert not dead, (
        "declared SOT owners that NOTHING in app/ calls. The registry says these "
        "own a decision; the codebase says otherwise. Wire it up, or strike it "
        "from the registry — a map that names the wrong owner is worse than no "
        "map:\n  " + "\n  ".join(sorted(dead))
    )


def test_the_dead_owner_list_only_shrinks() -> None:
    """A module that has come back to life must be removed from the exemption.

    Otherwise the exemption quietly forgives a module that no longer needs
    forgiving, and the next dead owner hides behind it.
    """
    graph = _import_graph()
    resurrected = [
        module for module in KNOWN_DEAD_OWNERS if _has_real_caller(module, graph)
    ]
    assert not resurrected, (
        "these owners now HAVE callers — remove them from KNOWN_DEAD_OWNERS so "
        "the guard protects them:\n  " + "\n  ".join(sorted(resurrected))
    )


def test_no_module_is_declared_under_two_owner_names() -> None:
    """One module, one name.

    The registry is meant to be a map from decision to owner. A module declared
    twice under different names with different `owns` sets means the map has two
    answers for "who owns this module", and the reader cannot tell which claim is
    load-bearing.
    """
    seen: dict[str, list[str]] = {}
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            seen.setdefault(service.module, []).append(service.name)

    duplicates = {module: names for module, names in seen.items() if len(names) > 1}
    # Today's duplicates, recorded so the property can be enforced going forward.
    # This may only shrink.
    known = {
        "app.services.access_resolution",
        "app.services.network.radius_sessions",
        "app.services.enforcement",
        "app.services.domain_settings",
    }
    unexpected = {
        module: names for module, names in duplicates.items() if module not in known
    }
    assert not unexpected, (
        "module declared under two owner names — the map now has two answers for "
        "who owns it:\n  "
        + "\n  ".join(f"{m}: {', '.join(n)}" for m, n in sorted(unexpected.items()))
    )
