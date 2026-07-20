"""Grant-preservation contracts for the granular reports RBAC migrations."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
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


def _grant_keys(connection, table, holder_column: str, holder_id: str) -> set[str]:
    permissions = sa.table("permissions", sa.column("id"), sa.column("key"))
    return set(
        connection.execute(
            sa.select(permissions.c.key)
            .select_from(
                table.join(permissions, table.c.permission_id == permissions.c.id)
            )
            .where(getattr(table.c, holder_column) == holder_id)
        ).scalars()
    )


def test_reports_permission_migrations_form_the_single_head_chain():
    granular = _load("reports_granular_chain", "370_reports_granular_permissions.py")
    retire = _load("reports_retire_chain", "371_retire_coarse_reports_permissions.py")
    current = _load(
        "rbac_catalog_identity_chain", "372_rbac_catalog_normalized_identity.py"
    )

    assert granular.down_revision == "369_vendor_lifecycle_evidence"
    assert retire.down_revision == granular.revision
    assert current.down_revision == retire.revision

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [current.revision]


def test_upgrade_and_rollback_preserve_role_and_direct_grants(monkeypatch):
    granular = _load("reports_granular_grants", "370_reports_granular_permissions.py")
    retire = _load("reports_retire_grants", "371_retire_coarse_reports_permissions.py")
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
        sa.UniqueConstraint("role_id", "permission_id"),
    )
    subscriber_permissions = sa.Table(
        "subscriber_permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("subscriber_id", sa.String, nullable=False),
        sa.Column("permission_id", sa.String, nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by_subscriber_id", sa.String),
        sa.UniqueConstraint("subscriber_id", "permission_id"),
    )
    system_user_permissions = sa.Table(
        "system_user_permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("system_user_id", sa.String, nullable=False),
        sa.Column("permission_id", sa.String, nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by_system_user_id", sa.String),
        sa.UniqueConstraint("system_user_id", "permission_id"),
    )
    metadata.create_all(engine)

    coarse_key = "reports:billing"
    coarse_id = str(uuid4())
    role_id = str(uuid4())
    subscriber_id = str(uuid4())
    system_user_id = str(uuid4())
    now = datetime.now(UTC)
    expected_granular = {"reports:billing:read", "reports:billing:export"}

    with engine.begin() as connection:
        connection.execute(
            permissions.insert(),
            [
                {
                    "id": coarse_id,
                    "key": coarse_key,
                    "description": "Billing reports",
                    "is_active": True,
                    "is_ui_assignable": True,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        connection.execute(
            role_permissions.insert(),
            {"id": str(uuid4()), "role_id": role_id, "permission_id": coarse_id},
        )
        connection.execute(
            subscriber_permissions.insert(),
            {
                "id": str(uuid4()),
                "subscriber_id": subscriber_id,
                "permission_id": coarse_id,
                "granted_at": now,
                "granted_by_subscriber_id": None,
            },
        )
        connection.execute(
            system_user_permissions.insert(),
            {
                "id": str(uuid4()),
                "system_user_id": system_user_id,
                "permission_id": coarse_id,
                "granted_at": now,
                "granted_by_system_user_id": None,
            },
        )
        monkeypatch.setattr(granular.op, "get_bind", lambda: connection)
        monkeypatch.setattr(retire.op, "get_bind", lambda: connection)

        granular.upgrade()
        assert (
            _grant_keys(connection, role_permissions, "role_id", role_id)
            >= expected_granular
        )
        assert (
            _grant_keys(
                connection, subscriber_permissions, "subscriber_id", subscriber_id
            )
            >= expected_granular
        )
        assert (
            _grant_keys(
                connection, system_user_permissions, "system_user_id", system_user_id
            )
            >= expected_granular
        )

        retire.upgrade()
        assert (
            connection.execute(
                sa.select(permissions.c.id).where(permissions.c.key == coarse_key)
            ).scalar_one_or_none()
            is None
        )

        retire.downgrade()
        for table, holder_column, holder_id in (
            (role_permissions, "role_id", role_id),
            (subscriber_permissions, "subscriber_id", subscriber_id),
            (system_user_permissions, "system_user_id", system_user_id),
        ):
            assert coarse_key in _grant_keys(
                connection, table, holder_column, holder_id
            )

        granular.downgrade()
        remaining_keys = set(connection.execute(sa.select(permissions.c.key)).scalars())
        assert coarse_key in remaining_keys
        assert not (remaining_keys & expected_granular)
        for table, holder_column, holder_id in (
            (role_permissions, "role_id", role_id),
            (subscriber_permissions, "subscriber_id", subscriber_id),
            (system_user_permissions, "system_user_id", system_user_id),
        ):
            assert coarse_key in _grant_keys(
                connection, table, holder_column, holder_id
            )
