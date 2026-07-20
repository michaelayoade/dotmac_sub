from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLANNER = PROJECT_ROOT / "app/services/network/fiber_field_verification_job_plans.py"
SCOPE = PROJECT_ROOT / "app/services/network/fiber_field_verification_job_scope.py"
WORKLIST = PROJECT_ROOT / "app/services/network/fiber_topology_field_worklist.py"
OBSERVATIONS = (
    PROJECT_ROOT / "app/services/network/fiber_topology_field_observations.py"
)
API = PROJECT_ROOT / "app/api/dispatch.py"
ADMIN = PROJECT_ROOT / "app/web/admin/network_fiber_plant.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_job_plan_owner_composes_evidence_and_delegates_every_job_write():
    source = PLANNER.read_text(encoding="utf-8")
    imported = _imports(PLANNER)

    assert "reconcile_fiber_field_worklist" in imported
    assert "work_order_commands" in imported
    assert "stage_audit_event" in imported
    assert "work_order_commands.create(" in source
    assert "work_order_commands.assign(" in source
    assert "work_order_commands.validate_subscriber_target(" in source
    assert "commit=False" in source
    assert "expected_plan_sha256" in source
    assert "expected_worklist_report_sha256" in source
    assert "WorkOrder(" not in source
    assert "WorkOrderAssignmentQueue(" not in source
    assert "FiberTopologyStagedFeature(" not in source


def test_planned_scope_is_exact_and_observation_owner_enforces_it():
    scope_source = SCOPE.read_text(encoding="utf-8")
    observation_source = OBSERVATIONS.read_text(encoding="utf-8")

    assert 'PLAN_METADATA_KEY = "fiber_field_verification_plan"' in scope_source
    assert "staged_feature_id" in scope_source
    assert "content_sha256" in scope_source
    assert "assert_feature_in_planned_scope(work_order, feature)" in (
        observation_source
    )
    for forbidden in ("external_id ==", "display_name ==", "ST_Distance", "distance"):
        assert forbidden not in scope_source


def test_worklist_and_map_admin_remain_read_only_and_have_no_plan_writer():
    worklist_source = WORKLIST.read_text(encoding="utf-8")
    admin_source = ADMIN.read_text(encoding="utf-8")

    assert "fiber_field_verification_job_plans" not in worklist_source
    assert "work_order_commands" not in worklist_source
    assert "preview_fiber_field_verification_job_plan" not in admin_source
    assert "execute_fiber_field_verification_job_plan" not in admin_source
    assert '@router.get(\n    "/fiber-field-verification"' in admin_source
    assert '@router.get(\n    "/fiber-field-verification-map"' in admin_source


def test_dispatch_api_is_permissioned_thin_plan_adapter():
    source = API.read_text(encoding="utf-8")

    assert '"/field-verification-job-plans/preview"' in source
    assert '"/field-verification-job-plans/execute"' in source
    assert 'require_permission("network:fiber:read")' in source
    assert 'require_permission("operations:dispatch:write")' in source
    assert 'require_permission("operations:dispatch:assign")' in source
    assert "preview_fiber_field_verification_job_plan(" in source
    assert "execute_fiber_field_verification_job_plan(" in source
    assert "WorkOrder(" not in source
    assert "WorkOrderAssignmentQueue(" not in source


def test_field_verification_job_planning_adds_no_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("346*fiber*field*verification*job*.py")
    )


def test_only_job_plan_owner_supplies_protected_work_order_metadata():
    callers = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path == PLANNER or path.name == "work_order_commands.py":
            continue
        if "owner_metadata=" in path.read_text(encoding="utf-8"):
            callers.append(str(path.relative_to(PROJECT_ROOT)))

    assert callers == []
    assert "owner_metadata=metadata" in PLANNER.read_text(encoding="utf-8")
