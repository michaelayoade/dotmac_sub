"""Add catalogue-driven usage allowance reset cycles.

Revision ID: 617_usage_allowance_reset_cycles
Revises: 617_billing_mode_transition_permission
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "617_usage_allowance_reset_cycles"
down_revision: str | None = "617_billing_mode_transition_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


reset_basis_enum = postgresql.ENUM(
    "calendar_month",
    "renewal_cycle",
    name="usage_allowance_reset_basis",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        reset_basis_enum.create(bind, checkfirst=True)

    op.add_column(
        "usage_allowances",
        sa.Column(
            "reset_basis",
            reset_basis_enum,
            nullable=False,
            server_default="calendar_month",
        ),
    )
    op.add_column(
        "usage_allowances",
        sa.Column("validity_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "usage_allowances",
        sa.Column(
            "rollover_validity_cycles",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # Existing capped high-speed data products are the renewal-cycle family.
    # Other allowances retain the legacy calendar-month behavior.
    op.execute(
        sa.text(
            """
            UPDATE usage_allowances AS allowance
               SET reset_basis = 'renewal_cycle',
                   validity_days = 30
             WHERE allowance.included_gb IS NOT NULL
               AND EXISTS (
                    SELECT 1
                      FROM catalog_offers AS offer
                     WHERE offer.usage_allowance_id = allowance.id
                       AND offer.plan_family = 'high_speed_data'
               )
            """
        )
    )
    op.create_check_constraint(
        "ck_usage_allowances_renewal_validity",
        "usage_allowances",
        "reset_basis != 'renewal_cycle' OR validity_days IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_usage_allowances_rollover_one_cycle",
        "usage_allowances",
        "rollover_validity_cycles = 1",
    )

    op.add_column(
        "quota_buckets",
        sa.Column(
            "usage_floor_gb",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "quota_buckets",
        sa.Column(
            "usage_allowance_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "quota_buckets",
        sa.Column(
            "rollover_origin_bucket_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_quota_buckets_usage_allowance_id",
        "quota_buckets",
        "usage_allowances",
        ["usage_allowance_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_quota_buckets_rollover_origin",
        "quota_buckets",
        "quota_buckets",
        ["rollover_origin_bucket_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_quota_buckets_usage_allowance_id",
        "quota_buckets",
        ["usage_allowance_id"],
    )

    op.create_table(
        "quota_session_baselines",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("quota_bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "radius_accounting_session_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("input_octets", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_octets", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["quota_bucket_id"],
            ["quota_buckets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["radius_accounting_session_id"],
            ["radius_accounting_sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "quota_bucket_id",
            "radius_accounting_session_id",
            name="uq_quota_session_baselines_bucket_session",
        ),
    )


def downgrade() -> None:
    op.drop_table("quota_session_baselines")
    op.drop_index("ix_quota_buckets_usage_allowance_id", table_name="quota_buckets")
    op.drop_constraint(
        "fk_quota_buckets_rollover_origin",
        "quota_buckets",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_quota_buckets_usage_allowance_id",
        "quota_buckets",
        type_="foreignkey",
    )
    op.drop_column("quota_buckets", "rollover_origin_bucket_id")
    op.drop_column("quota_buckets", "usage_allowance_id")
    op.drop_column("quota_buckets", "usage_floor_gb")
    op.drop_constraint(
        "ck_usage_allowances_rollover_one_cycle",
        "usage_allowances",
        type_="check",
    )
    op.drop_constraint(
        "ck_usage_allowances_renewal_validity",
        "usage_allowances",
        type_="check",
    )
    op.drop_column("usage_allowances", "rollover_validity_cycles")
    op.drop_column("usage_allowances", "validity_days")
    op.drop_column("usage_allowances", "reset_basis")
    if op.get_bind().dialect.name == "postgresql":
        reset_basis_enum.drop(op.get_bind(), checkfirst=True)
