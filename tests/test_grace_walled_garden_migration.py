"""Migration contract for canonical grace and restriction tiers."""

import importlib.util
from pathlib import Path

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "304_grace_walled_garden_policy.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "grace_walled_garden_migration", _MIGRATION_PATH
)
assert _SPEC and _SPEC.loader
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


def test_revision_advances_financial_access_evidence_chain():
    assert migration.revision == "304_grace_walled_garden_policy"
    assert migration.down_revision == "303_payment_import_batch_reversal"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_migration_persists_fail_closed_tier_and_retires_duplicate_timers():
    source = _MIGRATION_PATH.read_text()
    assert 'server_default="hard_reject"' in source
    assert 'sa.Column("access_mode", _access_mode, nullable=True)' in source
    assert "'prepaid_grace_days', 'prepaid_deactivation_days'" in source
