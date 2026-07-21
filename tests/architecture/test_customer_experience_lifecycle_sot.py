from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def test_customer_lifecycle_projection_is_read_only():
    path = APP / "services/customer_experience_lifecycle.py"
    tree = ast.parse(path.read_text())
    prohibited = {"add", "delete", "commit", "flush", "execute"}
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in prohibited
    ]
    assert calls == []


def test_customer_work_surfaces_use_native_projection_and_selfcare_owner():
    for relative in (
        "api/me.py",
        "web/customer/projects.py",
        "web/customer/work_orders.py",
        "services/reseller_crm_views.py",
    ):
        source = (APP / relative).read_text()
        assert "projects_mirror" not in source
        assert "work_orders_mirror" not in source
    assert (
        "customer_experience_lifecycle.projects_for_subscriber"
        in (APP / "api/me.py").read_text()
    )
    assert (
        "customer_work_order_selfcare.rate_technician"
        in (APP / "api/me.py").read_text()
    )


def test_project_task_relationship_is_guarded_by_work_order_command_owner():
    owner = (APP / "services/work_order_commands.py").read_text()
    assert "validate_project_task_target" in owner
    assert "project_task_binding_immutable" in owner
    assert 'data["project_task_id"] = normalized_task_id' in owner

    constructors = []
    for path in (APP / "services").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WorkOrder"
            for node in ast.walk(tree)
        ):
            constructors.append(path.relative_to(ROOT).as_posix())
    assert constructors == ["app/services/work_order_commands.py"]


def test_retired_work_mirror_runtime_is_absent():
    for relative in (
        "models/project_mirror.py",
        "services/projects_mirror.py",
        "services/work_orders_mirror.py",
        "tasks/projects.py",
        "tasks/work_orders.py",
    ):
        assert not (APP / relative).exists()


def test_crm_connector_cannot_read_project_or_work_order_authority():
    forbidden = (
        "list_work_orders",
        "get_work_order",
        "list_work_order_notes",
        "get_portal_projects",
        "get_portal_work_orders",
        "get_portal_technician_location",
    )
    for relative in (
        "services/crm_client.py",
        "services/integrations/crm_capability.py",
        "services/integrations/connectors/dotmac_crm.py",
    ):
        source = (APP / relative).read_text()
        for operation in forbidden:
            assert operation not in source, f"{relative} exposes {operation}"
