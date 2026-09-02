from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_migration():
    path = (
        PROJECT_ROOT / "alembic" / "versions" / "573_ticket_assignment_role_grants.py"
    )
    spec = importlib.util.spec_from_file_location("ticket_assignment_role_grants", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ticket_assignment_role_grants_are_idempotent_and_bounded() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    roles = sa.Table(
        "roles",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
    )
    permissions = sa.Table(
        "permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("key", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
    )
    role_permissions = sa.Table(
        "role_permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("role_id", sa.String, nullable=False),
        sa.Column("permission_id", sa.String, nullable=False),
        sa.UniqueConstraint("role_id", "permission_id"),
    )
    metadata.create_all(engine)

    assign_permission_id = str(uuid4())
    update_permission_id = str(uuid4())
    role_ids = {
        name: str(uuid4())
        for name in (
            "support",
            "Technical support",
            "Project",
            "customer_experience_manager",
            "project_management_office",
            "update_free_role",
        )
    }
    with engine.begin() as connection:
        connection.execute(
            permissions.insert(),
            [
                {
                    "id": assign_permission_id,
                    "key": migration.ASSIGN_PERMISSION_KEY,
                    "is_active": True,
                },
                {
                    "id": update_permission_id,
                    "key": migration.UPDATE_PERMISSION_KEY,
                    "is_active": True,
                },
            ],
        )
        connection.execute(
            roles.insert(),
            [
                {"id": role_id, "name": name, "is_active": True}
                for name, role_id in role_ids.items()
            ],
        )
        update_role_names = {
            "support",
            "Technical support",
            "customer_experience_manager",
            "project_management_office",
        }
        connection.execute(
            role_permissions.insert(),
            [
                {
                    "id": str(uuid4()),
                    "role_id": role_ids[name],
                    "permission_id": update_permission_id,
                }
                for name in update_role_names
            ]
            + [
                {
                    "id": str(uuid4()),
                    "role_id": role_ids["project_management_office"],
                    "permission_id": assign_permission_id,
                }
            ],
        )

        with patch.object(migration.op, "get_bind", return_value=connection):
            migration.upgrade()
            migration.upgrade()

        granted_names = set(
            connection.execute(
                sa.select(roles.c.name)
                .join(role_permissions, role_permissions.c.role_id == roles.c.id)
                .where(role_permissions.c.permission_id == assign_permission_id)
            ).scalars()
        )

    assert granted_names == {
        "support",
        "Technical support",
        "customer_experience_manager",
    }
