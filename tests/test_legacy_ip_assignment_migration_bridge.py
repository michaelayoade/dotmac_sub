from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_migration(filename: str, module_name: str):  # noqa: ANN202
    path = REPO_ROOT / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployed_legacy_revision_is_restored_as_idempotent_operation() -> None:
    migration = _load_migration(
        "153_ip_assignments_subscription_owner.py",
        "migration_153_ip_assignments_subscription_owner",
    )

    assert migration.revision == "153_ip_assignments_subscription_owner"
    assert migration.down_revision == "152_subscriber_additional_routes"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "if not _has_column(TABLE, COLUMN)" in source
    assert "if not _has_index(TABLE" in source


def test_legacy_branch_merge_is_ancestor_of_current_head() -> None:
    migration = _load_migration(
        "368_merge_legacy_ip_assignments_branch.py",
        "migration_368_merge_legacy_ip_assignments_branch",
    )

    assert migration.revision == "368_merge_legacy_ip_assignments_branch"
    assert set(migration.down_revision) == {
        "367_reports_support_permission",
        "153_ip_assignments_subscription_owner",
    }
    vendor_evidence = _load_migration(
        "369_vendor_project_lifecycle_evidence.py",
        "migration_369_vendor_project_lifecycle_evidence",
    )
    assert vendor_evidence.down_revision == migration.revision
    reports_retire = _load_migration(
        "371_retire_coarse_reports_permissions.py",
        "migration_371_retire_coarse_reports_permissions",
    )
    assert reports_retire.down_revision == "370_reports_granular_permissions"
    vendor_payment = _load_migration(
        "372_vendor_purchase_invoice_payment_projection.py",
        "migration_372_vendor_purchase_invoice_payment_projection",
    )
    assert vendor_payment.down_revision == reports_retire.revision
    vendor_review = _load_migration(
        "373_vendor_lifecycle_review_evidence.py",
        "migration_373_vendor_lifecycle_review_evidence",
    )
    assert vendor_review.down_revision == vendor_payment.revision
    as_built_review = _load_migration(
        "374_as_built_review_evidence.py",
        "migration_374_as_built_review_evidence",
    )
    assert as_built_review.down_revision == vendor_review.revision
    current = _load_migration(
        "375_work_order_evidence_policy.py",
        "migration_375_work_order_evidence_policy",
    )
    assert current.down_revision == as_built_review.revision

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["380_integration_platform_cutover"]
    legacy = script.get_revision("153_ip_assignments_subscription_owner")
    assert legacy is not None
    assert "368_merge_legacy_ip_assignments_branch" in {
        revision.revision
        for revision in script.iterate_revisions(
            "368_merge_legacy_ip_assignments_branch",
            "153_ip_assignments_subscription_owner",
            inclusive=True,
        )
    }
