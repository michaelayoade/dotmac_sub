"""Guard recommendation authority and keep legacy inference out of the owner."""

import ast
from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_registry.registry import all_services, registry_validation_errors


def test_template_catalog_has_one_complete_read_only_owner() -> None:
    owners = tuple(
        service
        for service in all_services()
        if service.module == "app.services.work_order_templates"
    )
    assert len(owners) == 1
    owner = owners[0]
    assert owner.name == "operations.work_order_templates"
    assert owner.contract is not None
    assert owner.contract.transaction.mode == TransactionMode.NOT_APPLICABLE
    assert all(concern.role == OwnerRole.POLICY for concern in owner.contract.concerns)
    assert owner.contract.events is None
    assert owner.contract.projections == ()
    assert not registry_validation_errors()


def test_template_catalog_cannot_read_persistence_or_transport_legacy_values() -> None:
    source_path = (
        Path(__file__).resolve().parents[2] / "app/services/work_order_templates.py"
    )
    tree = ast.parse(source_path.read_text())
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports <= {
        "__future__",
        "dataclasses",
        "enum",
        "app.services.field.work_order_kind",
    }
    # Public recommendations consume only the typed query; a new legacy input
    # or transport/persistence dependency requires deliberate contract review.
    recommendation = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "recommend_template"
    )
    assert recommendation.args.args == []
    assert [argument.arg for argument in recommendation.args.kwonlyargs] == ["query"]
    assert recommendation.args.kwarg is None


def test_classification_has_one_writer_and_canonical_transaction_boundary() -> None:
    project_root = Path(__file__).resolve().parents[2]
    owner_path = project_root / "app/services/work_order_commands.py"
    tree = ast.parse(owner_path.read_text())
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WorkOrderCommands"
    )
    classify = next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == "classify"
    )
    calls = [node for node in ast.walk(classify) if isinstance(node, ast.Call)]
    assert (
        sum(
            isinstance(node.func, ast.Name) and node.func.id == "execute_owner_command"
            for node in calls
        )
        == 1
    )
    stage = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_classify_work_order"
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "begin", "begin_nested"}
        for node in ast.walk(stage)
    )
    # Existing legacy creators cannot infer or write classification. Their
    # callers retain the previous creation contract and transaction semantics.
    for name in (
        "create",
        "stage_infrastructure_work_order",
        "stage_automated_project_task_work_order",
    ):
        method = next(
            node
            for node in owner.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        assert not any(
            isinstance(node, ast.Attribute)
            and node.attr
            in {
                "purpose",
                "access_technology",
                "classification_source",
                "template_version",
            }
            for node in ast.walk(method)
        )
    writable_fields = {
        "purpose",
        "access_technology",
        "classification_source",
        "template_version",
    }
    writers: set[str] = set()
    for source in (project_root / "app/services").rglob("*.py"):
        source_tree = ast.parse(source.read_text())
        for node in ast.walk(source_tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, (ast.AnnAssign, ast.AugAssign))
                else []
            )
            if any(
                isinstance(target, ast.Attribute)
                and target.attr in writable_fields
                and isinstance(target.value, ast.Name)
                and target.value.id == "row"
                for target in targets
            ):
                writers.add(str(source.relative_to(project_root)))
    assert writers == {"app/services/work_order_commands.py"}
