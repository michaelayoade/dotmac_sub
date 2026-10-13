"""Architecture guardrails for declared source-of-truth owners.

The registry is an operational map, not merely documentation.  Every module it
names as an owner must be reachable from application or operator code.  A module
that is only imported by tests, or named by the registry itself, is not a live
owner.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from app.services import sot_relationships
from scripts.architecture import sot_debt

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
SCRIPT_DIR = PROJECT_ROOT / "scripts"
UNDECLARED_WRITER_BASELINE = Path(__file__).with_name("sot_writer_baseline.txt")

# Temporary debt only. Entries may be removed when an owner is wired or struck
# from the registry; adding an entry requires an explicit ownership decision.
KNOWN_DEAD_OWNERS: set[str] = set()


def _imported_modules(path: Path) -> set[str]:
    import ast

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


@cache
def _writer_modules() -> set[str]:
    return sot_debt.persistence_writer_modules()


@cache
def _declared_owner_modules() -> set[str]:
    return sot_debt.declared_owner_modules(sot_relationships.DOMAIN_SOT_RELATIONSHIPS)


@cache
def _undeclared_writer_baseline() -> set[str]:
    return sot_debt.read_name_baseline(UNDECLARED_WRITER_BASELINE)


@cache
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
        f"{service.name} -> {module}"
        for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS
        for service in domain.services
        for module in service.owner_modules
        if module not in KNOWN_DEAD_OWNERS and not _has_real_caller(module, graph)
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


def test_no_new_undeclared_persistence_writers() -> None:
    undeclared = _writer_modules() - _declared_owner_modules()
    new_writers = sorted(undeclared - _undeclared_writer_baseline())
    assert not new_writers, (
        "new persistence-writing service modules have no declared SOT owner. "
        "Register the canonical owner; update the debt baseline only after an "
        "explicit ownership review:\n  " + "\n  ".join(new_writers)
    )


def test_undeclared_writer_baseline_only_shrinks() -> None:
    undeclared = _writer_modules() - _declared_owner_modules()
    stale = sorted(_undeclared_writer_baseline() - undeclared)
    assert not stale, (
        "undeclared-writer debt was resolved or stopped writing; remove these "
        "entries from the shrink-only baseline:\n  " + "\n  ".join(stale)
    )


def test_no_module_is_declared_under_unexpected_owner_names() -> None:
    seen: dict[str, list[str]] = {}
    for domain in sot_relationships.DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            assert len(service.owner_modules) == len(set(service.owner_modules)), (
                f"{service.name} repeats an implementation module"
            )
            for module in service.owner_modules:
                assert (
                    PROJECT_ROOT / (module.replace(".", "/") + ".py")
                ).is_file() or (
                    PROJECT_ROOT / module.replace(".", "/") / "__init__.py"
                ).is_file(), f"{service.name} declares missing implementation {module}"
                seen.setdefault(module, []).append(service.name)

    duplicates = {module: names for module, names in seen.items() if len(names) > 1}
    known = {
        "app.services.domain_settings",
        "app.services.enforcement",
        "app.services.network.radius_sessions",
        # The same module exposes two fully contracted reseller boundaries: a
        # read-only list projection and an idempotent status-action coordinator.
        "app.services.reseller_portal",
        # Public owner commands and the explicitly registered flush-only
        # assignment participant share this module and distinct boundaries.
        "app.services.work_order_commands",
    }
    # These modules compose separately registered typed readers/participants
    # alongside retained legacy boundaries. Pin the exact split so a third
    # authority cannot enter through a broad module exemption.
    split_boundaries = {
        # Account commands and the typed dashboard projections are distinct
        # contracts implemented by the same legacy Subscribers service. Pin
        # both names so this cannot become a broad third-owner exemption.
        "app.services.subscriber": {
            "customer.accounts",
            "customer.dashboard_overview",
        },
        "app.services.customer_experience_communications": {
            "communications.work_order_assignment_updates",
            "communications.customer_experience_intents",
        },
        "app.services.customer_work_order_selfcare": {
            "customer.work_order_tracking",
            "customer.work_order_selfcare",
        },
    }
    for module, expected_names in split_boundaries.items():
        assert set(duplicates.get(module, ())) == expected_names
    unexpected = {
        module: names
        for module, names in duplicates.items()
        if module not in known and module not in split_boundaries
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


def test_additional_implementation_is_part_of_writer_coverage() -> None:
    from dataclasses import replace

    from app.services.sot_manifest import SOTService
    from app.services.sot_registry.model import DomainSOT

    service = SOTService(
        name="planted.owner",
        module="app.services.field.transitions",
        owns=("planted concern",),
        implementation_modules=("app.services.field.completion_review",),
    )
    domain = DomainSOT("planted", (service,), (), "one named owner")
    assert sot_debt.declared_owner_modules((domain,)) == {
        "app.services.field.transitions",
        "app.services.field.completion_review",
    }
    without_additional = replace(
        domain, services=(replace(service, implementation_modules=()),)
    )
    assert sot_debt.declared_owner_modules((without_additional,)) == {
        "app.services.field.transitions"
    }


def test_additional_implementation_cannot_be_claimed_by_another_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from app.services.sot_manifest import SOTService

    domains = sot_relationships.DOMAIN_SOT_RELATIONSHIPS
    duplicate = SOTService(
        name="planted.parallel_completion",
        module="app.services.field.completion_review",
        owns=("planted concern",),
    )
    monkeypatch.setattr(
        sot_relationships,
        "DOMAIN_SOT_RELATIONSHIPS",
        (replace(domains[0], services=(*domains[0].services, duplicate)), *domains[1:]),
    )
    with pytest.raises(
        AssertionError, match="module declared under multiple owner names"
    ):
        test_no_module_is_declared_under_unexpected_owner_names()
