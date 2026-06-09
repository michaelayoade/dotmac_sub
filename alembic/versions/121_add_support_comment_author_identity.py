"""Add explicit support ticket comment author identity.

Revision ID: 121_add_support_comment_author_identity
Revises: 120_add_idempotency_keys
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "121_add_support_comment_author_identity"
down_revision = "120_add_idempotency_keys"
branch_labels = None
depends_on = None

_TABLE = "support_ticket_comments"
_AUTHOR_TYPE = "author_type"
_AUTHOR_SYSTEM_USER_ID = "author_system_user_id"
_FK = "fk_support_ticket_comments_author_system_user_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _AUTHOR_TYPE not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                _AUTHOR_TYPE,
                sa.String(length=40),
                nullable=False,
                server_default="system",
            ),
        )
        op.execute(
            f"""
            UPDATE {_TABLE}
            SET {_AUTHOR_TYPE} = CASE
                WHEN author_person_id IS NOT NULL THEN 'customer'
                ELSE 'system'
            END
            """
        )
        op.alter_column(_TABLE, _AUTHOR_TYPE, server_default=None)

    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _AUTHOR_SYSTEM_USER_ID not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_AUTHOR_SYSTEM_USER_ID, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            _FK,
            _TABLE,
            "system_users",
            [_AUTHOR_SYSTEM_USER_ID],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _AUTHOR_SYSTEM_USER_ID in columns:
        try:
            op.drop_constraint(_FK, _TABLE, type_="foreignkey")
        except Exception:  # pragma: no cover
            pass
        op.drop_column(_TABLE, _AUTHOR_SYSTEM_USER_ID)
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _AUTHOR_TYPE in columns:
        op.drop_column(_TABLE, _AUTHOR_TYPE)
