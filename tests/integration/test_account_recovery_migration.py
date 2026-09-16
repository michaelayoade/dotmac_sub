"""Structural checks on the account-recovery migration chain.

These do NOT apply the migrations against a real database. Running the actual
upgrade/downgrade rehearsal against disposable Postgres is CI's job. This module verifies the
static properties that must hold before that rehearsal can even be
attempted: correct revision chaining, and that downgrade refuses rather than
silently dropping tombstones once rows exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "alembic" / "versions"
EVIDENCE = VERSIONS / "612_account_recovery_evidence.py"
PERMISSIONS = VERSIONS / "613_account_recovery_permissions.py"
BLOCKED_REFS = VERSIONS / "614_recovery_blocked_refs.py"


def _module_vars(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        for t in targets:
            if isinstance(t, ast.Name) and value is not None:
                try:
                    values[t.id] = ast.literal_eval(value)
                except Exception:
                    pass
    return values


def test_612_chains_off_the_verified_head() -> None:
    values = _module_vars(EVIDENCE)
    assert values["revision"] == "612_account_recovery_evidence"
    assert values["down_revision"] == "611_offer_versions_unique_version_number"


def test_613_chains_off_612() -> None:
    values = _module_vars(PERMISSIONS)
    assert values["revision"] == "613_account_recovery_permissions"
    assert values["down_revision"] == "612_account_recovery_evidence"


def test_614_chains_off_613() -> None:
    values = _module_vars(BLOCKED_REFS)
    assert values["revision"] == "614_recovery_blocked_refs"
    assert values["down_revision"] == "613_account_recovery_permissions"


def test_614_preserves_original_command_outcomes() -> None:
    source = BLOCKED_REFS.read_text(encoding="utf-8")
    assert '"account_recovery_blocked_preflight"' in source
    assert '"account_recovery_command_outcomes"' in source
    assert '"idempotency_id"' in source
    assert '"generation"' in source
    assert '"affected_subscription_ids"' in source
    downgrade = source[source.index("def downgrade") :]
    assert "durable command outcome evidence exists" in downgrade
    assert "durable preflight replay evidence exists" in downgrade


def test_no_other_migration_also_claims_611_as_its_parent() -> None:
    """Exactly one child of 611 — otherwise Alembic has two heads again."""
    claimants = []
    for path in VERSIONS.glob("*.py"):
        values = _module_vars(path)
        if values.get("down_revision") == "611_offer_versions_unique_version_number":
            claimants.append(path.name)
    assert claimants == ["612_account_recovery_evidence.py"], claimants


def test_evidence_migration_downgrade_refuses_once_rows_exist() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    downgrade = source[source.index("def downgrade") :]
    assert "raise RuntimeError" in downgrade
    assert "count" in downgrade


def test_evidence_migration_backfills_before_removing_legacy_keys() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    backfill_index = source.index("INSERT INTO account_recovery_records")
    pop_index = source.index("cleaned.pop(")
    assert backfill_index < pop_index, (
        "legacy metadata_ keys must only be removed after the typed row exists"
    )


def test_evidence_migration_never_narrows_tool_lineage_to_subscription_only() -> None:
    """Fail-closed: the retired cascade tool's rows must always include the
    non-subscription categories it could have touched, since the JSON
    snapshot never recorded invoice/payment/RADIUS/IP/ONT/splitter
    involvement at all."""
    source = EVIDENCE.read_text(encoding="utf-8")
    assert "_CASCADE_ALWAYS_AFFECTED" in source
    tool_branch = source[
        source.index('if metadata.get("recovery_deleted_at"):') : source.index(
            "record_id = conn.execute"
        )
    ]
    assert "affected.update(_CASCADE_ALWAYS_AFFECTED)" in tool_branch


def test_self_service_metadata_is_not_backfilled_or_stripped() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    assert "account_deletion_requested_at" not in source
    assert "account_deletion_reason" not in source


def test_purged_restore_tool_rows_refuse_backfill_before_metadata_cleanup() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    guard = source.index('if metadata.get("recovery_purged_at"):')
    mutation = source.index("INSERT INTO account_recovery_records")
    cleanup = source.index("cleaned.pop(key, None)")
    assert guard < mutation < cleanup
    assert "typed terminal Records disposition" in source


def test_backfill_uses_original_subscription_status_and_unknown_offer_version() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    assert 'subscription_snapshots.append((sub_id, item["status"]))' in source
    assert '"SELECT id, status, offer_version_id FROM subscriptions "' not in source
    assert '":status, NULL"' in source
