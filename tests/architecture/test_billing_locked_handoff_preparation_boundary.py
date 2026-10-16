"""Private composition cannot become a standalone authority or completion path."""

from __future__ import annotations

import ast
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from app.services.billing.legacy_handoff_preparation_contracts import (
    LockedLegacyHandoffStagedObservation,
    PrepareLockedLegacyHandoffRequest,
)
from app.services.sot_manifest import TransactionMode
from app.services.sot_registry.registry import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/services/billing/legacy_handoff_preparation.py"
IMPORT = "app.services.billing.legacy_handoff_preparation"


def _calls(tree: ast.AST) -> set[str]:
    return {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }


def _private_invocations(source: str) -> list[int]:
    """Track exact private callable imports, including aliased module imports."""
    tree = ast.parse(source)
    functions = {"_stage_locked_legacy_handoff"}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == IMPORT:
                functions.update(
                    item.asname or item.name
                    for item in node.names
                    if item.name == "_stage_locked_legacy_handoff"
                )
            if node.module == "app.services.billing":
                modules.update(
                    item.asname or item.name
                    for item in node.names
                    if item.name == "legacy_handoff_preparation"
                )
        if isinstance(node, ast.Import):
            modules.update(
                item.asname or item.name for item in node.names if item.name == IMPORT
            )
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id in functions
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "_stage_locked_legacy_handoff"
            and (
                ast.unparse(node.func.value) in modules
                or ast.unparse(node.func.value) == IMPORT
            )
        )
    ]


def test_preparation_remains_private_participant_without_completion_or_admission() -> (
    None
):
    tree = ast.parse(MODULE.read_text())
    assert not [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert not _calls(tree) & {
        "commit",
        "rollback",
        "begin",
        "begin_nested",
        "execute_owner_command",
        "execute_owner_savepoint",
        "create_engine",
        "sessionmaker",
        "Session",
        "seal_opening_window",
        "grant",
        "create_receipt",
    }
    service = service_relationship("runtime.billing_legacy_archive_fence")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert (
        "app.services.billing.legacy_handoff_preparation"
        in service.implementation_modules
    )
    assert "financial.invoices.opening_export" in service.depends_on
    assert "runtime.billing_ledger_handoff" not in {
        owner.name for owner in all_services()
    }
    for contract in (
        PrepareLockedLegacyHandoffRequest,
        LockedLegacyHandoffStagedObservation,
    ):
        assert is_dataclass(contract) and contract.__dataclass_params__.frozen
        assert not {field.name for field in fields(contract)} & {
            "ready",
            "authorized",
            "verified_image",
            "active",
            "activated",
            "receipt",
            "admission",
            "role_proof",
            "seal",
            "full_closure",
        }


def test_actual_root_and_complete_union_precede_capture_and_components() -> None:
    tree = ast.parse(MODULE.read_text())
    stage = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_stage_locked_legacy_handoff"
    )
    calls = [node for node in ast.walk(stage) if isinstance(node, ast.Call)]
    first_line = lambda name: min(node.lineno for node in calls if name in _calls(node))
    assert first_line("_root") < first_line("exec_driver_sql")
    assert first_line("_posture") < first_line("exec_driver_sql")
    assert first_line("_inventory") < first_line("exec_driver_sql")
    assert (
        first_line("exec_driver_sql")
        < first_line("_locked_relations")
        < first_line("capture_locked_billing_opening_population")
    )
    assert (
        first_line("capture_locked_billing_opening_population")
        < first_line("_install_legacy_archive_fence")
        < first_line("_install_legacy_retained_columns_fence")
    )
    assert (
        len(
            [
                node
                for node in calls
                if isinstance(node.func, ast.Attribute)
                and node.func.attr == "exec_driver_sql"
            ]
        )
        == 1
    )
    inventory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_inventory"
    )
    assert {
        "billing_opening_lock_inventory",
        "relay_lock_inventory",
        "sorted",
    } <= _calls(inventory)
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and node.value in {48, 54}
        for node in ast.walk(tree)
    )
    qualifier = (ROOT / "app/services/billing/legacy_archive_fence.py").read_text()
    assert "owner_command_active(db, owner=_OWNER)" in qualifier
    assert "current_owner_root_transaction(db) is not root" in qualifier
    assert '_OWNER = "runtime.billing_ledger_handoff"' in qualifier


def test_no_production_adapter_invokes_the_private_composition() -> None:
    for directory in ("app", "scripts", "alembic"):
        for path in (ROOT / directory).rglob("*.py"):
            assert not _private_invocations(path.read_text()), path


@pytest.mark.parametrize(
    "source",
    (
        "from app.services.billing.legacy_handoff_preparation import _stage_locked_legacy_handoff as run\nrun(db, request=request)",
        "import app.services.billing.legacy_handoff_preparation as prep\nprep._stage_locked_legacy_handoff(db, request=request)",
        "from app.services.billing import legacy_handoff_preparation as prep\nprep._stage_locked_legacy_handoff(db, request=request)",
        "import app.services.billing.legacy_handoff_preparation\napp.services.billing.legacy_handoff_preparation._stage_locked_legacy_handoff(db, request=request)",
    ),
)
def test_invocation_guard_detects_aliases_and_fully_qualified_paths(
    source: str,
) -> None:
    assert _private_invocations(source)


def test_semantic_comparison_excludes_only_capture_time_and_snapshot_labels() -> None:
    tree = ast.parse(MODULE.read_text())
    comparator = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_same_census"
    )
    replace_calls = [
        node
        for node in ast.walk(comparator)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replace"
    ]
    assert [{keyword.arg for keyword in node.keywords} for node in replace_calls] == [
        {"capture"},
        {"captured_at", "snapshot_id"},
    ]
    assert not _calls(comparator) & {"asdict", "hash", "sha256"}


def test_existing_collector_inventory_exposure_reads_no_population_and_takes_no_locks() -> (
    None
):
    tree = ast.parse((ROOT / "app/services/billing/opening_export.py").read_text())
    query = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "billing_opening_lock_inventory"
    )
    assert "_locked_population_relations" in _calls(query)
    assert not _calls(query) & {
        "execute",
        "exec_driver_sql",
        "_population",
        "_cash_evidence",
        "_documentary_observations",
    }
    routing = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_locked_population_relations"
    )
    assert not _calls(routing) & {"execute", "exec_driver_sql", "scalars"}
