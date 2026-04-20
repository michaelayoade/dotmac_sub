"""Add subscriber contacts.

Revision ID: 044_add_subscriber_contacts
Revises: 043_add_pending_tr069_job_status
Create Date: 2026-04-20
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "044_add_subscriber_contacts"
down_revision = "043_add_pending_tr069_job_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "subscriber_contacts" in inspector.get_table_names():
        return

    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "subscriber_contacts",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("subscriber_id", uuid_type, nullable=False),
        sa.Column("full_name", sa.String(length=160), nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("relationship", sa.String(length=80), nullable=True),
        sa.Column("contact_type", sa.String(length=40), nullable=False),
        sa.Column(
            "is_billing_contact",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_authorized",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "receives_notifications",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_id"], ["subscribers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_subscriber_contacts_subscriber_id",
        "subscriber_contacts",
        ["subscriber_id"],
    )
    op.create_index("ix_subscriber_contacts_email", "subscriber_contacts", ["email"])
    op.create_index("ix_subscriber_contacts_phone", "subscriber_contacts", ["phone"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "subscriber_contacts" not in inspector.get_table_names():
        return

    op.drop_index("ix_subscriber_contacts_phone", table_name="subscriber_contacts")
    op.drop_index("ix_subscriber_contacts_email", table_name="subscriber_contacts")
    op.drop_index(
        "ix_subscriber_contacts_subscriber_id", table_name="subscriber_contacts"
    )
    op.drop_table("subscriber_contacts")
