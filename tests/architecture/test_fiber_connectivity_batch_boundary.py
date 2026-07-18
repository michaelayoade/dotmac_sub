from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_connectivity_review.py"
COMMAND = PROJECT_ROOT / "scripts/network/review_fiber_topology_connectivity_batch.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/344_fiber_connectivity_batch_control.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _subcommands(tree: ast.Module) -> set[str]:
    commands = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, (ast.Tuple, ast.List))
        ):
            continue
        dynamic_parser_call = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "add_parser"
            and child.args
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id == node.target.id
            for child in ast.walk(node)
        )
        if dynamic_parser_call:
            commands.update(
                item.value
                for item in node.iter.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return commands


def test_batch_owner_delegates_every_decision_transition():
    source = SERVICE.read_text()
    imported = _imported_names(_tree(SERVICE))

    for forbidden_model in (
        "FiberChangeRequest",
        "FiberSegment",
        "FiberTerminationPoint",
        "FdhCabinet",
        "FiberAccessPoint",
    ):
        assert forbidden_model not in imported
    for forbidden_inference in (
        "ST_Distance",
        "nearest",
        "snap_to",
        "gps_latitude",
        "gps_longitude",
    ):
        assert forbidden_inference not in source
    assert "FiberTopologyConnectivityDecision(" not in source
    assert "propose_connectivity_decision(" in source
    assert "approve_connectivity_decision(" in source
    assert "decline_connectivity_decision(" in source
    assert "execute_connectivity_decision(" in source
    assert "finalize_connectivity_decision(" in source
    assert "approve_request(" not in source


def test_batch_cli_exposes_reviewed_bounded_modes_only():
    assert _subcommands(_tree(COMMAND)) == {
        "preview",
        "propose",
        "inspect",
        "approve",
        "decline",
        "execute",
        "reconcile",
    }
    source = COMMAND.read_text()
    assert "--expected-manifest-sha256" in source
    assert "--limit" in source
    assert "bulk-approve" not in source


def test_migration_adds_only_batch_evidence_and_decision_provenance():
    tree = _tree(MIGRATION)
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    op_calls = [
        node
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    created_tables = {
        node.args[0].value
        for node in op_calls
        if node.func.attr == "create_table"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    assert created_tables == {
        "fiber_topology_connectivity_proposal_batches",
        "fiber_topology_connectivity_batch_reviews",
        "fiber_topology_connectivity_runs",
    }
    assert {node.func.attr for node in op_calls}.isdisjoint(
        {"execute", "alter_column", "drop_table"}
    )
