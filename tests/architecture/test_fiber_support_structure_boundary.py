from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app/services/network/fiber_support_structures.py"
CHANGE_OWNER = PROJECT_ROOT / "app/services/fiber_change_requests.py"
IDENTITY_OWNER = PROJECT_ROOT / "app/services/network/fiber_topology_identity.py"
COVERAGE_OWNER = (
    PROJECT_ROOT / "app/services/network/fiber_topology_identity_coverage.py"
)
COMMAND = PROJECT_ROOT / "scripts/network/review_fiber_support_mount.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/358_fiber_support_structures.py"


def _constructors(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_support_owner_is_the_only_application_constructor_and_mount_writer():
    expected = {
        "FiberSupportStructure",
        "FiberSupportMountDecision",
        "FiberSupportMount",
    }
    writers: dict[str, set[str]] = {}
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        found = _constructors(path).intersection(expected)
        if found:
            writers[str(path.relative_to(PROJECT_ROOT))] = found

    assert writers == {
        "app/services/network/fiber_support_structures.py": expected,
    }


def test_reviewed_asset_change_and_identity_owners_delegate_support_mutations():
    change_source = CHANGE_OWNER.read_text(encoding="utf-8")
    identity_source = IDENTITY_OWNER.read_text(encoding="utf-8")

    assert '"support_structure": FiberSupportStructure' in change_source
    assert "apply_reviewed_support_change(" in change_source
    assert '"support_structure"' in identity_source
    assert "fiber_change_requests.create_request(" in identity_source
    assert "FiberSupportStructure(" not in change_source
    assert "FiberSupportStructure(" not in identity_source

    application_callers = {
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "app").rglob("*.py")
        if path != OWNER and "apply_reviewed_support_change(" in path.read_text()
    }
    assert application_callers == {"app/services/fiber_change_requests.py"}


def test_mount_owner_requires_preview_review_revalidation_and_exact_result_evidence():
    source = OWNER.read_text(encoding="utf-8")

    for required in (
        "preview_mount_decision",
        "expected_decision_sha256",
        "proposer cannot review",
        "with_for_update",
        "expected_support_state_sha256",
        "expected_asset_state_sha256",
        "expected_mount_state_sha256",
        "result_payload",
        "result_sha256",
        'outcome="closed_stale"',
        'outcome="closed_conflict"',
        "stage_audit_event(",
    ):
        assert required in source
    for forbidden in (
        "ST_Distance",
        "distance(",
        "nearest",
        "display_name",
        "external_id",
    ):
        assert forbidden not in source


def test_coverage_treats_support_as_canonical_without_becoming_a_writer():
    source = COVERAGE_OWNER.read_text(encoding="utf-8")

    assert '"support_structure": FiberSupportStructure' in source
    assert "REJECT_ONLY_TYPES" not in source
    assert "reject_only_point_count" not in source
    assert '"support_structure_identities_terminal_current"' in source
    assert "FiberSupportStructure(" not in source
    assert "FiberSupportMount(" not in source
    assert "propose_mount_decision" not in source
    assert "execute_mount_decision" not in source


def test_operator_command_is_thin_and_exposes_no_unreviewed_apply_mode():
    source = COMMAND.read_text(encoding="utf-8")

    for command in ("preview", "propose", "approve", "decline", "execute", "inspect"):
        assert f'"{command}"' in source
    assert "expected-decision-sha256" in source
    assert "preview_mount_decision(" in source
    assert "propose_mount_decision(" in source
    assert "review_mount_decision(" in source
    assert "execute_mount_decision(" in source
    assert "FiberSupportStructure(" not in source
    assert "FiberSupportMount(" not in source
    assert "db.add(" not in source
    assert "db.commit(" not in source


def test_migration_adds_only_support_identity_and_mount_evidence_tables():
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "358_fiber_support_structures"' in source
    assert 'down_revision = "357_account_credit_deposit_lifecycle"' in source
    assert '"fiber_support_structures"' in source
    assert '"fiber_support_mount_decisions"' in source
    assert '"fiber_support_mounts"' in source
    assert '"uq_fiber_support_mount_decisions_active_asset"' in source
    assert '"ck_fiber_support_mount_decisions_result_evidence"' in source
    assert '"ck_fiber_support_mounts_shape"' in source
    assert "fiber_segments" not in source
    assert "fiber_access_points" not in source
    assert "fiber_topology_staged_features" not in source
