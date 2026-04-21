"""Store subscriber contact channels without required names.

Revision ID: 045_contact_channels_without_name
Revises: 044_add_subscriber_contacts
Create Date: 2026-04-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "045_contact_channels_without_name"
down_revision = "044_add_subscriber_contacts"
branch_labels = None
depends_on = None


_TABLE = "subscriber_contacts"
_COLUMNS = {
    "whatsapp": sa.Column("whatsapp", sa.String(length=80), nullable=True),
    "facebook": sa.Column("facebook", sa.String(length=160), nullable=True),
    "instagram": sa.Column("instagram", sa.String(length=160), nullable=True),
    "x_handle": sa.Column("x_handle", sa.String(length=160), nullable=True),
    "telegram": sa.Column("telegram", sa.String(length=160), nullable=True),
    "linkedin": sa.Column("linkedin", sa.String(length=160), nullable=True),
    "other_social": sa.Column("other_social", sa.Text(), nullable=True),
}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _TABLE not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, column in _COLUMNS.items():
        if name not in existing:
            op.add_column(_TABLE, column)

    if "full_name" in existing:
        op.alter_column(
            _TABLE,
            "full_name",
            existing_type=sa.String(length=160),
            nullable=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if _TABLE not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "full_name" in existing:
        op.execute("UPDATE subscriber_contacts SET full_name = '' WHERE full_name IS NULL")
        op.alter_column(
            _TABLE,
            "full_name",
            existing_type=sa.String(length=160),
            nullable=False,
        )

    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
