from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICES = PROJECT_ROOT / "app/services"
OWNER = SERVICES / "work_order_commands.py"
IMPORTER = SERVICES / "work_orders_mirror.py"
DISPATCH = SERVICES / "dispatch.py"
MANAGER = SERVICES / "field/manager.py"
WEB = SERVICES / "web_dispatch_work_orders.py"


def _constructors(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_work_order_rows_have_one_native_constructor_and_one_import_boundary():
    native_constructors: list[str] = []
    queue_constructors: list[str] = []
    for path in SERVICES.rglob("*.py"):
        constructors = _constructors(path)
        if "WorkOrder" in constructors:
            native_constructors.append(str(path.relative_to(PROJECT_ROOT)))
        if "WorkOrderAssignmentQueue" in constructors:
            queue_constructors.append(str(path.relative_to(PROJECT_ROOT)))

    assert sorted(native_constructors) == [
        "app/services/work_order_commands.py",
        "app/services/work_orders_mirror.py",
    ]
    assert queue_constructors == ["app/services/work_order_commands.py"]


def test_assignment_projection_has_no_parallel_service_writer():
    assignment_fields = {
        "assigned_to_crm_person_id",
        "assigned_to_name",
        "technician_name",
        "assigned_technician_id",
    }
    writers: set[str] = set()
    for path in SERVICES.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        targets = [
            target
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        ]
        if any(
            isinstance(target, ast.Attribute) and target.attr in assignment_fields
            for target in targets
        ):
            writers.add(str(path.relative_to(PROJECT_ROOT)))

    assert writers == {
        "app/services/work_order_commands.py",
        "app/services/work_orders_mirror.py",
    }


def test_dispatch_adapters_delegate_and_cannot_write_assignment_state_directly():
    dispatch_source = DISPATCH.read_text(encoding="utf-8")
    manager_source = MANAGER.read_text(encoding="utf-8")
    web_source = WEB.read_text(encoding="utf-8")

    assert "work_order_commands.create(" in dispatch_source
    assert "work_order_commands.update_header(" in dispatch_source
    assert "work_order_commands.create_queue_entry(" in dispatch_source
    assert "work_order_commands.update_queue_entry(" in dispatch_source
    assert "work_order_commands.assign(" in manager_source
    assert "assigned_to_name=" not in web_source
    for source in (dispatch_source, manager_source, web_source):
        assert "WorkOrder(" not in source
        assert "WorkOrderAssignmentQueue(" not in source


def test_crm_import_is_the_only_non_command_work_order_writer():
    importer_source = IMPORTER.read_text(encoding="utf-8")
    assert "WorkOrder(" in importer_source
    assert "crm_work_order_id=" in importer_source
    assert "work_order_commands" not in importer_source


def test_assignment_readers_ignore_non_assigned_queue_rows():
    for relative_path in (
        "field/jobs.py",
        "field/manager.py",
        "work_orders_mirror.py",
        "workqueue/providers/work_orders.py",
    ):
        source = (SERVICES / relative_path).read_text(encoding="utf-8")
        assert "DispatchQueueStatus.assigned" in source
