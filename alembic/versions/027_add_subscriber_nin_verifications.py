"""Add subscriber NIN verifications.

Revision ID: 027_add_subscriber_nin_verifications
Revises: 026_add_ont_wan_service_instances
Create Date: 2026-04-17

"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "027_add_subscriber_nin_verifications"
down_revision = "026_add_ont_wan_service_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    status_enum = postgresql.ENUM(
        "pending",
        "success",
        "failed",
        name="subscriber_nin_verification_status",
        create_type=False,
    )
    status_enum.create(bind, checkfirst=True)

    table_exists = "subscriber_nin_verifications" in inspector.get_table_names()
    if not table_exists:
        op.create_table(
            "subscriber_nin_verifications",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("nin", sa.String(length=11), nullable=False),
            sa.Column(
                "status",
                status_enum,
                nullable=False,
                server_default="pending",
            ),
            sa.Column("is_match", sa.Boolean(), nullable=True),
            sa.Column("match_score", sa.Integer(), nullable=True),
            sa.Column(
                "mono_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["subscriber_id"],
                ["subscribers.id"],
                name="fk_subscriber_nin_verifications_subscriber_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {
        index["name"]
        for index in inspect(bind).get_indexes("subscriber_nin_verifications")
    }
    if "ix_subscriber_nin_verifications_subscriber_id" not in existing_indexes:
        op.create_index(
            "ix_subscriber_nin_verifications_subscriber_id",
            "subscriber_nin_verifications",
            ["subscriber_id"],
        )
    if "ix_subscriber_nin_verifications_nin" not in existing_indexes:
        op.create_index(
            "ix_subscriber_nin_verifications_nin",
            "subscriber_nin_verifications",
            ["nin"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "subscriber_nin_verifications" in inspector.get_table_names():
        indexes = {
            index["name"]
            for index in inspector.get_indexes("subscriber_nin_verifications")
        }
        if "ix_subscriber_nin_verifications_nin" in indexes:
            op.drop_index(
                "ix_subscriber_nin_verifications_nin",
                table_name="subscriber_nin_verifications",
            )
        if "ix_subscriber_nin_verifications_subscriber_id" in indexes:
            op.drop_index(
                "ix_subscriber_nin_verifications_subscriber_id",
                table_name="subscriber_nin_verifications",
            )
        op.drop_table("subscriber_nin_verifications")
    postgresql.ENUM(
        "pending",
        "success",
        "failed",
        name="subscriber_nin_verification_status",
        create_type=False,
    ).drop(op.get_bind(), checkfirst=True)
