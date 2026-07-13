"""Architecture guardrails for declared source-of-truth owners.

The registry is an operational map, not merely documentation.  Every module it
names as an owner must be reachable from application or operator code.  A module
that is only imported by tests, or named by the registry itself, is not a live
owner.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
SCRIPT_DIR = PROJECT_ROOT / "scripts"

# Temporary debt only. Entries may be removed when an owner is wired or struck
# from the registry; adding an entry requires an explicit ownership decision.
KNOWN_DEAD_OWNERS: set[str] = set()


def _imported_modules(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - syntax checks fail elsewhere
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
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
    for importer, imports in graph.items():
        if importer == module or importer.endswith("sot_relationships"):
            continue
        if module in imports:
            return True
    return False


def test_every_declared_owner_has_a_real_caller() -> None:
    graph = _import_graph()
    dead = [
        f"{service.name} -> {service.module}"
        for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS
        for service in domain.services
        if service.module not in KNOWN_DEAD_OWNERS
        and not _has_real_caller(service.module, graph)
    ]

    assert not dead, (
        "declared SOT owners that application and operator code never call. "
        "Wire each owner, or remove the false ownership claim:\n  "
        + "\n  ".join(sorted(dead))
    )


def test_the_dead_owner_list_only_shrinks() -> None:
    graph = _import_graph()
    resurrected = [
        module for module in KNOWN_DEAD_OWNERS if _has_real_caller(module, graph)
    ]
    assert not resurrected, (
        "owners now have callers; remove their liveness exemptions:\n  "
        + "\n  ".join(sorted(resurrected))
    )


def test_no_module_is_declared_under_unexpected_owner_names() -> None:
    seen: dict[str, list[str]] = {}
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            seen.setdefault(service.module, []).append(service.name)

    duplicates = {module: names for module, names in seen.items() if len(names) > 1}
    known = {
        "app.services.access_resolution",
        "app.services.domain_settings",
        "app.services.enforcement",
        "app.services.network.radius_sessions",
    }
    unexpected = {
        module: names for module, names in duplicates.items() if module not in known
    }
    stale_exemptions = sorted(known - duplicates.keys())
    assert not stale_exemptions, (
        "duplicate-owner exemptions that no longer describe the registry:\n  "
        + "\n  ".join(stale_exemptions)
    )
    assert not unexpected, (
        "module declared under multiple owner names:\n  "
        + "\n  ".join(
            f"{module}: {', '.join(names)}"
            for module, names in sorted(unexpected.items())
        )
    )
