"""add ont_provisioning_events table

Revision ID: 038_add_ont_provisioning_events
Revises: 037_add_saga_executions
Create Date: 2026-04-19

Adds an append-only event log for ONT provisioning step outcomes.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "038_add_ont_provisioning_events"
down_revision = "037_add_saga_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "ont_provisioning_events" in inspector.get_table_names():
        return

    event_status = postgresql.ENUM(
        "succeeded",
        "failed",
        "skipped",
        "waiting",
        name="ontprovisioningeventstatus",
        create_type=False,
    )
    event_status.create(conn, checkfirst=True)

    op.create_table(
        "ont_provisioning_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ont_unit_id", sa.UUID(), nullable=False),
        sa.Column("step_name", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("status", event_status, nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("event_data", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "compensation_applied",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("correlation_key", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ont_provisioning_events_ont_unit",
        "ont_provisioning_events",
        ["ont_unit_id"],
    )
    op.create_index(
        "ix_ont_provisioning_events_step_name",
        "ont_provisioning_events",
        ["step_name"],
    )
    op.create_index(
        "ix_ont_provisioning_events_action",
        "ont_provisioning_events",
        ["action"],
    )
    op.create_index(
        "ix_ont_provisioning_events_status",
        "ont_provisioning_events",
        ["status"],
    )
    op.create_index(
        "ix_ont_provisioning_events_correlation_key",
        "ont_provisioning_events",
        ["correlation_key"],
    )
    op.create_index(
        "ix_ont_provisioning_events_created_at",
        "ont_provisioning_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_provisioning_events_created_at",
        table_name="ont_provisioning_events",
    )
    op.drop_index(
        "ix_ont_provisioning_events_status",
        table_name="ont_provisioning_events",
    )
    op.drop_index(
        "ix_ont_provisioning_events_correlation_key",
        table_name="ont_provisioning_events",
    )
    op.drop_index(
        "ix_ont_provisioning_events_action",
        table_name="ont_provisioning_events",
    )
    op.drop_index(
        "ix_ont_provisioning_events_step_name",
        table_name="ont_provisioning_events",
    )
    op.drop_index(
        "ix_ont_provisioning_events_ont_unit",
        table_name="ont_provisioning_events",
    )
    op.drop_table("ont_provisioning_events")
