"""Contracts for assignable Workqueue audience permission seeds."""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_FILE = "570_seed_workqueue_audience_permissions.py"
TEAM_KEY = "workqueue:audience:team"
ORG_KEY = "workqueue:audience:org"


def _load(name: str, file_name: str):
    path = REPO_ROOT / "alembic" / "versions" / file_name
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(engine) -> dict[str, sa.Table]:
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
    tables = {"permissions": permissions}
    for table_name, holder_column in (
        ("role_permissions", "role_id"),
        ("subscriber_permissions", "subscriber_id"),
        ("system_user_permissions", "system_user_id"),
    ):
        tables[table_name] = sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column(holder_column, sa.String, nullable=False),
            sa.Column("permission_id", sa.String, nullable=False),
            sa.UniqueConstraint(holder_column, "permission_id"),
        )
    metadata.create_all(engine)
    return tables


def test_migration_extends_the_current_host_head() -> None:
    migration = _load("workqueue_audience_chain", MIGRATION_FILE)
    parent = _load("workqueue_audience_parent", "569_retire_crm_chat_authority.py")

    assert migration.down_revision == parent.revision


def test_upgrade_seeds_active_ui_assignable_permissions_without_grants(
    monkeypatch,
) -> None:
    migration = _load("workqueue_audience_upgrade", MIGRATION_FILE)
    engine = sa.create_engine("sqlite://")
    tables = _schema(engine)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            tables["permissions"].insert(),
            {
                "id": str(uuid4()),
                "key": TEAM_KEY,
                "description": "stale description",
                "is_active": False,
                "is_ui_assignable": False,
                "created_at": now,
                "updated_at": now,
            },
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()
        migration.upgrade()

        rows = connection.execute(
            sa.select(
                tables["permissions"].c.key,
                tables["permissions"].c.description,
                tables["permissions"].c.is_active,
                tables["permissions"].c.is_ui_assignable,
            ).where(tables["permissions"].c.key.in_((TEAM_KEY, ORG_KEY)))
        ).mappings()
        seeded = {row["key"]: dict(row) for row in rows}

        assert set(seeded) == {TEAM_KEY, ORG_KEY}
        assert all(row["is_active"] is True for row in seeded.values())
        assert all(row["is_ui_assignable"] is True for row in seeded.values())
        assert "queue lead" in seeded[TEAM_KEY]["description"]
        assert (
            "regardless of team, region, or assignment"
            in seeded[ORG_KEY]["description"]
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(tables["role_permissions"])
            ).scalar_one()
            == 0
        )


def test_downgrade_removes_manual_grants_before_permission_rows(monkeypatch) -> None:
    migration = _load("workqueue_audience_downgrade", MIGRATION_FILE)
    engine = sa.create_engine("sqlite://")
    tables = _schema(engine)

    with engine.begin() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        permission_ids = dict(
            connection.execute(
                sa.select(
                    tables["permissions"].c.key,
                    tables["permissions"].c.id,
                ).where(tables["permissions"].c.key.in_((TEAM_KEY, ORG_KEY)))
            ).all()
        )
        connection.execute(
            tables["role_permissions"].insert(),
            {
                "id": str(uuid4()),
                "role_id": str(uuid4()),
                "permission_id": permission_ids[ORG_KEY],
            },
        )

        migration.downgrade()

        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(tables["role_permissions"])
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(tables["permissions"])
                .where(tables["permissions"].c.key.in_((TEAM_KEY, ORG_KEY)))
            ).scalar_one()
            == 0
        )


def test_permissions_are_registered_in_ui_seed_catalog() -> None:
    seed_path = REPO_ROOT / "scripts" / "seed" / "seed_rbac.py"
    module = ast.parse(seed_path.read_text(encoding="utf-8"), filename=str(seed_path))
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id in {"ADMIN_ONLY_PERMISSION_KEYS", "DEFAULT_PERMISSIONS"}
    }

    seeded = {key for key, _description in assignments["DEFAULT_PERMISSIONS"]}
    admin_only = assignments["ADMIN_ONLY_PERMISSION_KEYS"]
    assert {TEAM_KEY, ORG_KEY} <= seeded
    assert TEAM_KEY not in admin_only
    assert ORG_KEY not in admin_only
