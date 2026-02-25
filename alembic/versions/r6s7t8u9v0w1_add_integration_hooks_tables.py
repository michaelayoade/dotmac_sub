"""Add integration hooks tables.

Revision ID: r6s7t8u9v0w1
Revises: n4p5q6r7s8t9
Create Date: 2026-02-25 11:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "r6s7t8u9v0w1"
down_revision = "n4p5q6r7s8t9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    hook_type_enum = postgresql.ENUM(
        "web",
        "cli",
        "internal",
        name="integrationhooktype",
        create_type=False,
    )
    hook_auth_type_enum = postgresql.ENUM(
        "none",
        "bearer",
        "basic",
        "hmac",
        name="integrationhookauthtype",
        create_type=False,
    )
    hook_exec_status_enum = postgresql.ENUM(
        "success",
        "failed",
        name="integrationhookexecutionstatus",
        create_type=False,
    )

    hook_type_enum.create(op.get_bind(), checkfirst=True)
    hook_auth_type_enum.create(op.get_bind(), checkfirst=True)
    hook_exec_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "integration_hooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("hook_type", hook_type_enum, nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=600), nullable=True),
        sa.Column("http_method", sa.String(length=10), nullable=False),
        sa.Column("auth_type", hook_auth_type_enum, nullable=False),
        sa.Column("auth_config", sa.JSON(), nullable=True),
        sa.Column("retry_max", sa.Integer(), nullable=False),
        sa.Column("retry_backoff_ms", sa.Integer(), nullable=False),
        sa.Column("event_filters", sa.JSON(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_integration_hooks_hook_type", "integration_hooks", ["hook_type"])
    op.create_index("ix_integration_hooks_is_enabled", "integration_hooks", ["is_enabled"])

    op.create_table(
        "integration_hook_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hook_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("status", hook_exec_status_enum, nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["hook_id"], ["integration_hooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_integration_hook_executions_hook_id",
        "integration_hook_executions",
        ["hook_id"],
    )
    op.create_index(
        "ix_integration_hook_executions_created_at",
        "integration_hook_executions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_hook_executions_created_at", table_name="integration_hook_executions")
    op.drop_index("ix_integration_hook_executions_hook_id", table_name="integration_hook_executions")
    op.drop_table("integration_hook_executions")
    op.drop_index("ix_integration_hooks_is_enabled", table_name="integration_hooks")
    op.drop_index("ix_integration_hooks_hook_type", table_name="integration_hooks")
    op.drop_table("integration_hooks")
    op.execute(sa.text("DROP TYPE IF EXISTS integrationhookexecutionstatus"))
    op.execute(sa.text("DROP TYPE IF EXISTS integrationhookauthtype"))
    op.execute(sa.text("DROP TYPE IF EXISTS integrationhooktype"))
