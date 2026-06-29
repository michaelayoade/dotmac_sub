"""Add referral_mirror + referral_program_cache (local copy of CRM referrals).

Revision ID: 186_add_referral_mirror
Revises: 186_connector_auth_config_text
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "186_add_referral_mirror"
down_revision = "186_connector_auth_config_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_mirror",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_referral_id", sa.String(length=64), nullable=False),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("referred_name", sa.String(length=160), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("reward_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "reward_currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column(
            "reward_status", sa.String(length=20), nullable=False, server_default="none"
        ),
        sa.Column("referral_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rewarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_referral_mirror_crm_referral_id", "referral_mirror", ["crm_referral_id"]
    )
    op.create_index(
        "ix_referral_mirror_subscriber_id", "referral_mirror", ["subscriber_id"]
    )

    op.create_table(
        "referral_program_cache",
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("share_url", sa.String(length=255), nullable=False),
        sa.Column(
            "program_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("reward_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "reward_currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("referral_program_cache")
    op.drop_index("ix_referral_mirror_subscriber_id", table_name="referral_mirror")
    op.drop_constraint(
        "uq_referral_mirror_crm_referral_id", "referral_mirror", type_="unique"
    )
    op.drop_table("referral_mirror")
