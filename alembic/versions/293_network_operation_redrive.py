"""Add safe network-operation redrive lineage.

Revision ID: 293_network_operation_redrive
Revises: 292_merge_lifecycle_schedules_and_tax_point_heads
Create Date: 2026-07-14
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "293_network_operation_redrive"
down_revision = "292_merge_lifecycle_schedules_and_tax_point_heads"
branch_labels = None
depends_on = None

_TABLE = "network_operations"
_PERMISSION_KEY = "network:operation:redrive"
_PERMISSION_DESCRIPTION = "Retry eligible failed network operations"


def _seed_permission() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_PERMISSION_KEY,
                description=_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )

    admin_id = bind.execute(
        sa.select(roles.c.id).where(
            roles.c.name == "admin",
            roles.c.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if admin_id is None:
        return
    existing = bind.execute(
        sa.select(role_permissions.c.id).where(
            role_permissions.c.role_id == admin_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            role_permissions.insert().values(
                id=uuid4(),
                role_id=admin_id,
                permission_id=permission_id,
            )
        )


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "redrive_of_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("redrive_reason", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("redrive_reviewed_head", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("redrive_idempotency_key", sa.String(length=160), nullable=True)
        )
        batch.create_foreign_key(
            "fk_network_operations_redrive_of_id",
            _TABLE,
            ["redrive_of_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_netops_redrive_of", ["redrive_of_id"])
        batch.create_index(
            "uq_netops_redrive_idempotency",
            ["redrive_of_id", "redrive_idempotency_key"],
            unique=True,
        )
    _seed_permission()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if {"permissions", "role_permissions"}.issubset(inspector.get_table_names()):
        metadata = sa.MetaData()
        permissions = sa.Table("permissions", metadata, autoload_with=bind)
        role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
        permission_id = bind.execute(
            sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
        ).scalar_one_or_none()
        if permission_id is not None:
            bind.execute(
                role_permissions.delete().where(
                    role_permissions.c.permission_id == permission_id
                )
            )
            bind.execute(permissions.delete().where(permissions.c.id == permission_id))

    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_index("uq_netops_redrive_idempotency")
        batch.drop_index("ix_netops_redrive_of")
        batch.drop_constraint(
            "fk_network_operations_redrive_of_id",
            type_="foreignkey",
        )
        batch.drop_column("redrive_idempotency_key")
        batch.drop_column("redrive_reviewed_head")
        batch.drop_column("redrive_reason")
        batch.drop_column("redrive_of_id")
