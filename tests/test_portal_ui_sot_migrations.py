"""Migration contracts for the portal UI source-of-truth remediation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, file_name: str):
    path = REPO_ROOT / "alembic" / "versions" / file_name
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portal_ui_migrations_extend_current_main_as_one_head():
    migration_360 = _load("migration_360", "360_gis_granular_permissions.py")
    migration_361 = _load("migration_361", "361_retire_coarse_gis_edit_permission.py")
    migration_362 = _load("migration_362", "362_reports_support_permission.py")

    assert migration_360.down_revision == "359_payment_prepaid_applications"
    assert migration_361.down_revision == migration_360.revision
    assert migration_362.down_revision == migration_361.revision

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [migration_362.revision]


def test_coarse_gis_permission_downgrade_restores_role_grants(monkeypatch):
    migration = _load(
        "migration_361_rollback", "361_retire_coarse_gis_edit_permission.py"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    permissions = sa.Table(
        "permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("key", sa.String, unique=True, nullable=False),
        sa.Column("description", sa.String),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("is_ui_assignable", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    role_permissions = sa.Table(
        "role_permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("role_id", sa.String, nullable=False),
        sa.Column("permission_id", sa.String, nullable=False),
    )
    metadata.create_all(engine)

    role_id = str(uuid4())
    coarse_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            permissions.insert(),
            [
                {
                    "id": coarse_id,
                    "key": migration.COARSE_KEY,
                    "description": migration.COARSE_DESCRIPTION,
                    "is_active": True,
                    "is_ui_assignable": True,
                },
                *[
                    {
                        "id": str(uuid4()),
                        "key": key,
                        "description": key,
                        "is_active": True,
                        "is_ui_assignable": True,
                    }
                    for key in migration.GRANULAR_KEYS
                ],
            ],
        )
        granular_ids = connection.execute(
            sa.select(permissions.c.id).where(
                permissions.c.key.in_(migration.GRANULAR_KEYS)
            )
        ).scalars()
        connection.execute(
            role_permissions.insert(),
            [
                {"id": str(uuid4()), "role_id": role_id, "permission_id": pid}
                for pid in granular_ids
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()
        assert (
            connection.execute(
                sa.select(permissions.c.id).where(
                    permissions.c.key == migration.COARSE_KEY
                )
            ).scalar_one_or_none()
            is None
        )

        migration.downgrade()
        restored_coarse_id = connection.execute(
            sa.select(permissions.c.id).where(permissions.c.key == migration.COARSE_KEY)
        ).scalar_one()
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(role_permissions)
                .where(
                    role_permissions.c.role_id == role_id,
                    role_permissions.c.permission_id == restored_coarse_id,
                )
            ).scalar_one()
            == 1
        )
