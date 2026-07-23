"""Architecture guards for the durable TR-069 command lifecycle."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from app.services.scheduler import (
    EVENT_DRIVEN_TRANSPORT_TASKS,
    PERMANENT_LIFECYCLE_TASKS,
)
from app.services.sot_manifest import contract_validation_errors

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
OWNER = Path("app/services/network/tr069_job_commands.py")
MODEL = Path("app/models/tr069.py")


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_tr069_command_owner_has_a_complete_valid_manifest_contract() -> None:
    services = {service.name: service for service in sot_relationships.all_services()}
    owner = services["network.tr069_commands"]

    assert owner.module == "app.services.network.tr069_job_commands"
    assert owner.contract is not None
    assert not contract_validation_errors(owner, service_names=set(services))
    assert owner.contract.migration.fallback_retirement is not None


def test_tr069_jobs_have_one_application_writer() -> None:
    violations: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT)
        if relative in {OWNER, MODEL}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "Tr069Job":
                violations.append(f"{relative}:{node.lineno}:constructor")
    assert not violations, (
        "TR-069 job lifecycle rows must be created by network.tr069_commands:\n"
        + "\n".join(violations)
    )


def test_no_legacy_tr069_command_producer_or_adoption_path_remains() -> None:
    application = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(APP_DIR.rglob("*.py"))
    )

    assert "execute_bulk_action as tr069_execute_bulk_action" not in application
    assert 'name="app.tasks.tr069.execute_bulk_action"' not in application
    assert "adopt_legacy_tr069_job" not in application
    assert "Tr069JobCreate" not in application
    assert "Tr069JobUpdate" not in application
    assert "network.tr069_job_execution" not in application
    assert "tr069_job_execution_enabled" not in application


def test_admission_and_drainage_are_separate_scheduler_responsibilities() -> None:
    assert "app.tasks.tr069.reconcile_command_outcomes" in PERMANENT_LIFECYCLE_TASKS
    assert (
        "app.tasks.network_operation_dispatch.publish_network_operation_dispatches"
        in PERMANENT_LIFECYCLE_TASKS
    )
    assert (
        "app.tasks.tr069.execute_network_operation_job" in EVENT_DRIVEN_TRANSPORT_TASKS
    )

    scheduler_source = (PROJECT_ROOT / "app/services/scheduler_config.py").read_text(
        encoding="utf-8"
    )
    assert 'name="tr069_command_reconciler"' in scheduler_source
    assert "enabled=True" in scheduler_source


def test_migration_terminalizes_pre_cutover_rows_and_clears_payloads() -> None:
    migration = (
        PROJECT_ROOT / "alembic/versions/409_tr069_operation_lifecycle.py"
    ).read_text(encoding="utf-8")

    assert "status = 'failed'" in migration
    assert "status = 'unverified'" in migration
    assert "WHERE network_operation_id IS NULL" in migration
    assert "payload = NULL" in migration
    assert "network_tr069_command_admission" in migration
    assert "network_tr069_job_execution" in migration


def test_public_projection_is_redacted_and_terminal_state_clears_secret() -> None:
    owner = (PROJECT_ROOT / OWNER).read_text(encoding="utf-8")
    model = (PROJECT_ROOT / MODEL).read_text(encoding="utf-8")

    assert "secure_payload" in model
    assert "EncryptedJSON" in model
    assert '"values": "[redacted]"' in owner
    assert '"url": "[redacted]"' in owner
    assert "job.secure_payload = None" in owner
    rotation = (PROJECT_ROOT / "app/services/credential_key_rotation.py").read_text(
        encoding="utf-8"
    )
    assert '"Tr069Job.secure_payload"' in rotation

    tasks = (PROJECT_ROOT / "app/tasks/tr069.py").read_text(encoding="utf-8")
    assert "db.delete(job)" not in tasks
    operation_cleanup = (PROJECT_ROOT / "app/tasks/network_operations.py").read_text(
        encoding="utf-8"
    )
    assert "NetworkOperationType.cpe_tr069_command" in operation_cleanup
