"""Add structural remote-reprovision verification evidence.

Revision ID: 402_remote_reprovision_verification
Revises: 401_service_change_execution_chain
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "402_remote_reprovision_verification"
down_revision = "401_service_change_execution_chain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column, target, key in (
        ("remote_radius_profile_id", "radius_profiles", "remote_radius_profile"),
        ("remote_radius_user_id", "radius_users", "remote_radius_user"),
    ):
        op.add_column(
            "subscription_change_requests",
            sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_sub_change_{key}",
            "subscription_change_requests",
            target,
            [column],
            ["id"],
            ondelete="RESTRICT",
        )
    op.add_column(
        "subscription_change_requests",
        sa.Column(
            "remote_reprovision_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("subscription_change_requests", "remote_reprovision_requested_at")
    for column, key in (
        ("remote_radius_user_id", "remote_radius_user"),
        ("remote_radius_profile_id", "remote_radius_profile"),
    ):
        op.drop_constraint(
            f"fk_sub_change_{key}",
            "subscription_change_requests",
            type_="foreignkey",
        )
        op.drop_column("subscription_change_requests", column)
