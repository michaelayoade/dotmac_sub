"""Add cutover balance variance registry.

Revision ID: 207_add_cutover_balance_variances
Revises: 206_add_fdh_target_to_outage_incidents
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "207_add_cutover_balance_variances"
down_revision = "206_add_fdh_target_to_outage_incidents"
branch_labels = None
depends_on = None

TABLE = "cutover_balance_variances"


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _has_index(table: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table))


def upgrade() -> None:
    if not _has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", UUID(as_uuid=True), nullable=False),
            sa.Column("account_id", UUID(as_uuid=True), nullable=False),
            sa.Column("expected_drift", sa.Numeric(12, 2), nullable=False),
            sa.Column("direction", sa.String(length=20), nullable=False),
            sa.Column("reason", sa.String(length=120), nullable=False),
            sa.Column("evidence_ref", sa.Text(), nullable=False),
            sa.Column("approved_by", sa.String(length=120), nullable=False),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "direction IN ('overcredited', 'understated')",
                name="ck_cutover_balance_variances_direction",
            ),
            sa.CheckConstraint(
                "status IN ('accepted', 'superseded', 'rejected')",
                name="ck_cutover_balance_variances_status",
            ),
            sa.ForeignKeyConstraint(["account_id"], ["subscribers.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index(TABLE, "ix_cutover_balance_variances_account_id"):
        op.create_index(
            "ix_cutover_balance_variances_account_id",
            TABLE,
            ["account_id"],
            unique=False,
        )
    if not _has_index(TABLE, "uq_cutover_balance_variances_active_account"):
        op.create_index(
            "uq_cutover_balance_variances_active_account",
            TABLE,
            ["account_id"],
            unique=True,
            postgresql_where=sa.text("is_active AND status = 'accepted'"),
        )


def downgrade() -> None:
    if _has_index(TABLE, "uq_cutover_balance_variances_active_account"):
        op.drop_index(
            "uq_cutover_balance_variances_active_account",
            table_name=TABLE,
        )
    if _has_index(TABLE, "ix_cutover_balance_variances_account_id"):
        op.drop_index("ix_cutover_balance_variances_account_id", table_name=TABLE)
    if _has_table(TABLE):
        op.drop_table(TABLE)
