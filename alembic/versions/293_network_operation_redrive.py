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
    op.add_column(
        _TABLE,
        sa.Column(
            "redrive_of_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("redrive_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("redrive_reviewed_head", sa.String(length=64), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("redrive_idempotency_key", sa.String(length=160), nullable=True),
    )
    op.create_foreign_key(
        "fk_network_operations_redrive_of_id",
        _TABLE,
        _TABLE,
        ["redrive_of_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_netops_redrive_of",
        _TABLE,
        ["redrive_of_id"],
    )
    op.create_index(
        "uq_netops_redrive_idempotency",
        _TABLE,
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

    op.drop_index("uq_netops_redrive_idempotency", table_name=_TABLE)
    op.drop_index("ix_netops_redrive_of", table_name=_TABLE)
    op.drop_constraint(
        "fk_network_operations_redrive_of_id",
        _TABLE,
        type_="foreignkey",
    )
    op.drop_column(_TABLE, "redrive_idempotency_key")
    op.drop_column(_TABLE, "redrive_reviewed_head")
    op.drop_column(_TABLE, "redrive_reason")
    op.drop_column(_TABLE, "redrive_of_id")
