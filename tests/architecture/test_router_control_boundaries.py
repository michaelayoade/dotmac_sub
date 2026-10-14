"""Router control plane PR1 guards (design §6.3: 5, 8, 10, 11, 12 and read-only)."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

from app.services.composition.plugin import plugin_problems
from app.services.router_control.plugin import ROUTER_PLUGIN
from app.services.router_control.surfaces import claim_problems

ROOT = Path(__file__).resolve().parents[2]
COMPOSITION = ROOT / "app" / "services" / "composition"
ROUTER_CONTROL = ROOT / "app" / "services" / "router_control"
SCRIPT = ROOT / "scripts" / "network" / "router_control_report.py"
ALLOWED_EXTERNAL = {"pydantic"}
STDLIB = set(sys.stdlib_module_names)


def _modules(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def _imports(path: Path) -> list[tuple[int, str, int]]:
    """(line, module, level) for each import statement."""
    tree = ast.parse(path.read_text())
    found: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, node.module or "", node.level))
    return found


def test_composition_core_is_independent():
    """Guard 8: stdlib + pydantic + itself only; nothing from `app` outside the package."""
    offenders: list[str] = []
    for path in _modules(COMPOSITION):
        for line, module, level in _imports(path):
            top = module.split(".")[0]
            if level > 0:
                if level > 1:
                    offenders.append(
                        f"{path.name}:{line} relative import leaves the package"
                    )
                continue
            if module == "app.services.composition" or module.startswith(
                "app.services.composition."
            ):
                continue
            if top in STDLIB or top in ALLOWED_EXTERNAL:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{line} imports {module}")
    assert not offenders, offenders


def test_import_linter_contract_for_the_composition_core_is_declared():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    named = [
        c for c in contracts if c["name"] == "Composition core must not import Sub"
    ]
    assert len(named) == 1
    contract = named[0]
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == ["app.services.composition"]
    assert "app.models" in contract["forbidden_modules"]
    assert "app.services.router_control" in contract["forbidden_modules"]


def test_router_control_is_a_read_only_resolver():
    """No sessions, models, router transports or writers in PR1."""
    forbidden_modules = (
        "app.models",
        "app.db",
        "app.services.router_management",
        "paramiko",
        "librouteros",
        "requests",
        "httpx",
        "sqlalchemy",
        "subprocess",
        "socket",
    )
    offenders: list[str] = []
    for path in _modules(ROUTER_CONTROL) + _modules(COMPOSITION):
        for line, module, level in _imports(path):
            if level == 0 and any(
                module == f or module.startswith(f + ".") for f in forbidden_modules
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{line} imports {module}")
        text = path.read_text()
        for token in (".commit(", ".flush(", ".execute(", ".rollback("):
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} uses {token}")
    assert not offenders, offenders


def test_router_control_never_reads_inventory_tags():
    """Guard 11: policy tags come from bindings; `routers.tags` never drives policy."""
    offenders: list[str] = []
    for path in _modules(ROUTER_CONTROL) + _modules(COMPOSITION):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "tags"
                and isinstance(node.value, ast.Name)
                and node.value.id in {"Router", "router", "row"}
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, offenders


def test_single_routeros_export_parser():
    """Guard 5: no new `/export` parser; this is a shrink-only list."""
    allowed = {
        "app/services/router_health/routeros_export.py",
        "app/services/walled_garden_router_readiness.py",
    }
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and re.fullmatch(
                r"_?parse_(routeros_)?export", node.name
            ):
                found.add(str(path.relative_to(ROOT)))
    assert found <= allowed, sorted(found - allowed)


def test_router_control_reuses_the_router_health_parser():
    text = "".join(path.read_text() for path in _modules(ROUTER_CONTROL))
    assert "app.services.router_health.routeros_export" in text
    assert not re.search(r"def _?parse_\w*export", text)


def test_module_field_specs_complete_and_registry_consistent():
    """Guard 10 plus the registry-load checks (derivation DAG, tiers, claims)."""
    assert plugin_problems(ROUTER_PLUGIN) == []
    assert claim_problems(ROUTER_PLUGIN) == []
    assert set(ROUTER_PLUGIN.modules) == {"snmp", "ntp", "services-hardening"}


def test_cli_script_uses_the_read_only_session_and_no_router_transport():
    text = SCRIPT.read_text()
    assert "read_only_snapshot_session()" in text
    modules = {module for _, module, _ in _imports(SCRIPT)}
    assert not any(
        m.startswith(
            ("app.services.router_management", "paramiko", "requests", "httpx")
        )
        for m in modules
    )
