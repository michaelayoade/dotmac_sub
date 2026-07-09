"""Allow device tokens for field system users.

Revision ID: 225_field_device_tokens
Revises: 224_add_field_setting_domain
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "225_field_device_tokens"
down_revision = "224_add_field_setting_domain"
branch_labels = None
depends_on = None

_TABLE = "device_tokens"


def _columns() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _TABLE not in inspect(bind).get_table_names():
        return
    columns = _columns()
    if "system_user_id" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_device_tokens_system_user_id",
            _TABLE,
            "system_users",
            ["system_user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index("ix_device_tokens_system_user_id", _TABLE, ["system_user_id"])
    if "app_version" not in columns:
        op.add_column(_TABLE, sa.Column("app_version", sa.String(length=40)))
    op.alter_column(_TABLE, "subscriber_id", nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _TABLE not in inspect(bind).get_table_names():
        return
    columns = _columns()
    op.execute("DELETE FROM device_tokens WHERE subscriber_id IS NULL")
    op.alter_column(_TABLE, "subscriber_id", nullable=False)
    if "app_version" in columns:
        op.drop_column(_TABLE, "app_version")
    if "system_user_id" in columns:
        op.drop_index("ix_device_tokens_system_user_id", table_name=_TABLE)
        op.drop_constraint(
            "fk_device_tokens_system_user_id", _TABLE, type_="foreignkey"
        )
        op.drop_column(_TABLE, "system_user_id")
