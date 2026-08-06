"""Migration-chain contract for positive ONT reconcile admission."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/487_ont_reconcile_positive_admission.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "ont_reconcile_positive_admission", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_positive_admission_is_the_single_linear_head():
    module = _module()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert module.revision == "487_ont_reconcile_positive_admission"
    assert module.down_revision == "486_service_handoffs"
    assert script.get_heads() == ["487_ont_reconcile_positive_admission"]


def test_schema_enforces_expiry_idempotency_and_one_active_admission():
    source = MIGRATION.read_text(encoding="utf-8")

    assert "expires_at > admitted_at" in source
    assert "uq_ont_reconcile_admissions_idempotency_key" in source
    assert "uq_ont_reconcile_admissions_active_per_ont_scope" in source
    assert "postgresql_where=sa.text(\"status = 'active'\")" in source
