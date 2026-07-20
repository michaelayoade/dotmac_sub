"""Architecture guards for durable network-operation command dispatch."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
OWNER = Path("app/services/network_operation_dispatch.py")
MODEL = Path("app/models/network_operation.py")
MIGRATED_ORIGINATORS = (
    Path("app/services/network_operation_recovery.py"),
    Path("app/services/network/ont_firmware.py"),
    Path("app/services/network/olt_firmware.py"),
    Path("app/services/network/ont_reconcile_queue.py"),
    Path("app/services/network/ont_provisioning_commands.py"),
    Path("app/services/network/ont_provisioning_execution.py"),
    Path("app/services/network/olt_api_operations.py"),
    Path("app/services/web_network_ont_actions/device_actions.py"),
    Path("app/tasks/ont_provisioning.py"),
    Path("app/tasks/tr069.py"),
    Path("app/web/admin/network_olts_inventory.py"),
    Path("app/web/admin/network_onts_provisioning.py"),
)
DISPATCH_FIELDS = {
    "status",
    "attempts",
    "next_attempt_at",
    "last_attempt_at",
    "dispatched_at",
    "acknowledged_at",
    "completed_at",
    "task_id",
    "last_error",
}
MANAGED_TARGETS = {
    Path("app/tasks/ont_runtime_status.py"): {
        "refresh_single_ont_status": (
            "app.tasks.ont_runtime_status.refresh_single_ont_status"
        ),
    },
    Path("app/tasks/ont_provisioning.py"): {
        "authorize_ont": "app.tasks.ont_provisioning.authorize_ont",
        "provision_ont": "app.tasks.ont_provisioning.provision_ont",
    },
    Path("app/tasks/tr069.py"): {
        "wait_for_ont_bootstrap": "app.tasks.tr069.wait_for_ont_bootstrap",
    },
    Path("app/tasks/ont_firmware.py"): {
        "apply_huawei_ont_firmware": (
            "app.tasks.ont_firmware.apply_huawei_ont_firmware"
        ),
    },
    Path("app/tasks/olt_firmware.py"): {
        "upgrade_firmware_task": "app.tasks.olt_firmware.upgrade_with_verification",
    },
    Path("app/tasks/ont_reconcile.py"): {
        "reconcile_huawei_ont": "app.tasks.ont_reconcile.reconcile_huawei_ont",
    },
}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_migrated_operation_originators_do_not_publish_directly() -> None:
    forbidden = {"enqueue_task", "delay", "apply_async", "send_task"}
    violations: list[str] = []
    for relative in MIGRATED_ORIGINATORS:
        tree = ast.parse(
            (PROJECT_ROOT / relative).read_text(encoding="utf-8"),
            filename=str(relative),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in forbidden:
                violations.append(f"{relative}:{node.lineno}:{_call_name(node)}")
    assert not violations, (
        "Operation-backed commands must stage through network.operation_dispatch:\n"
        + "\n".join(violations)
    )


def test_dispatch_rows_have_one_application_writer() -> None:
    violations: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT)
        if relative in {OWNER, MODEL}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "NetworkOperationDispatch"
            ):
                violations.append(f"{relative}:{node.lineno}:constructor")
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in DISPATCH_FIELDS
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "dispatch"
                ):
                    violations.append(f"{relative}:{node.lineno}:{target.attr}")
    assert not violations, (
        "Dispatch evidence must be written by network.operation_dispatch:\n"
        + "\n".join(violations)
    )


def test_managed_target_tasks_claim_dispatch_before_device_code() -> None:
    violations: list[str] = []
    for relative, expected_functions in MANAGED_TARGETS.items():
        tree = ast.parse(
            (PROJECT_ROOT / relative).read_text(encoding="utf-8"),
            filename=str(relative),
        )
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name, task_name in expected_functions.items():
            function = functions.get(function_name)
            if function is None:
                violations.append(f"{relative}:{function_name}:missing")
                continue
            claims = [
                decorator
                for decorator in function.decorator_list
                if isinstance(decorator, ast.Call)
                and _call_name(decorator) == "managed_network_operation_dispatch"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == task_name
            ]
            if not claims:
                violations.append(f"{relative}:{function.lineno}:{function_name}")
            argument_names = {
                argument.arg
                for argument in (
                    list(function.args.args) + list(function.args.kwonlyargs)
                )
            }
            if "_network_dispatch_id" not in argument_names:
                violations.append(
                    f"{relative}:{function.lineno}:{function_name}:missing_dispatch_arg"
                )
    assert not violations, (
        "Managed target task is missing the durable execution claim:\n"
        + "\n".join(violations)
    )


def test_provisioning_workers_do_not_create_operations() -> None:
    violations: list[str] = []
    for relative in (
        Path("app/tasks/ont_provisioning.py"),
        Path("app/tasks/tr069.py"),
    ):
        tree = ast.parse(
            (PROJECT_ROOT / relative).read_text(encoding="utf-8"),
            filename=str(relative),
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "network_operations"
                and node.func.attr == "start"
            ):
                violations.append(f"{relative}:{node.lineno}:network_operations.start")
    assert not violations, (
        "Provisioning operations must originate in the command service:\n"
        + "\n".join(violations)
    )
