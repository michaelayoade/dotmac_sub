from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.services.integrations.registry import require_connector_definition

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/414_adopt_paystack_manifest_pin.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_414_adopt_paystack_manifest_pin",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_adoption_migration_matches_deployed_paystack_definition() -> None:
    migration = _load_migration()
    definition = require_connector_definition("paystack")

    assert migration.revision == "414_adopt_paystack_manifest_pin"
    assert migration.down_revision == "413_audit_actor_label"
    assert migration.CONNECTOR_VERSION == definition.version
    assert migration.CONTROL_PLANE_DIGEST == definition.digest
    assert migration.PRE_CONTROL_PLANE_DIGEST != migration.CONTROL_PLANE_DIGEST


def test_manifest_adoption_updates_only_known_nonretired_paystack_pin(
    monkeypatch,
) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    installations = sa.Table(
        "integration_installations",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("connector_key", sa.String, nullable=False),
        sa.Column("connector_version", sa.String, nullable=False),
        sa.Column("manifest_digest", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("updated_at", sa.DateTime),
        sa.Column("updated_by", sa.String),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            installations.insert(),
            [
                {
                    "id": 1,
                    "connector_key": "paystack",
                    "connector_version": migration.CONNECTOR_VERSION,
                    "manifest_digest": migration.PRE_CONTROL_PLANE_DIGEST,
                    "state": "enabled",
                },
                {
                    "id": 2,
                    "connector_key": "paystack",
                    "connector_version": migration.CONNECTOR_VERSION,
                    "manifest_digest": "f" * 64,
                    "state": "enabled",
                },
                {
                    "id": 3,
                    "connector_key": "paystack",
                    "connector_version": migration.CONNECTOR_VERSION,
                    "manifest_digest": migration.PRE_CONTROL_PLANE_DIGEST,
                    "state": "retired",
                },
                {
                    "id": 4,
                    "connector_key": "dotmac.crm",
                    "connector_version": migration.CONNECTOR_VERSION,
                    "manifest_digest": migration.PRE_CONTROL_PLANE_DIGEST,
                    "state": "enabled",
                },
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        rows = {
            row.id: row
            for row in connection.execute(
                sa.select(installations).order_by(installations.c.id)
            )
        }

    assert rows[1].manifest_digest == migration.CONTROL_PLANE_DIGEST
    assert rows[1].updated_by == migration.MIGRATION_ACTOR
    assert rows[2].manifest_digest == "f" * 64
    assert rows[3].manifest_digest == migration.PRE_CONTROL_PLANE_DIGEST
    assert rows[4].manifest_digest == migration.PRE_CONTROL_PLANE_DIGEST


def test_manifest_adoption_downgrade_is_intentionally_blocked() -> None:
    migration = _load_migration()

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
