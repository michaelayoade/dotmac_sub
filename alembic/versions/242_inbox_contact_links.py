"""Add inbox contact links.

Revision ID: 242_inbox_contact_links
Revises: 241_operational_escalation_primitives
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "242_inbox_contact_links"
down_revision = "241_operational_escalation_primitives"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_contact_links"):
        return
    op.create_table(
        "inbox_contact_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("normalized_contact", sa.String(length=255), nullable=False),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reseller_id", postgresql.UUID(as_uuid=True)),
        sa.Column("linked_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["reseller_id"], ["resellers.id"]),
        sa.CheckConstraint(
            "(subscriber_id IS NOT NULL AND reseller_id IS NULL)"
            " OR (subscriber_id IS NULL AND reseller_id IS NOT NULL)",
            name="ck_inbox_contact_links_one_target",
        ),
    )
    op.create_index(
        "ix_inbox_contact_links_contact",
        "inbox_contact_links",
        ["channel_type", "normalized_contact", "is_active"],
    )
    op.create_index(
        "ix_inbox_contact_links_subscriber",
        "inbox_contact_links",
        ["subscriber_id", "is_active"],
    )
    op.create_index(
        "ix_inbox_contact_links_reseller",
        "inbox_contact_links",
        ["reseller_id", "is_active"],
    )
    op.create_index(
        "uq_inbox_contact_links_active_contact",
        "inbox_contact_links",
        ["channel_type", "normalized_contact"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_contact_links"):
        op.drop_table("inbox_contact_links")
