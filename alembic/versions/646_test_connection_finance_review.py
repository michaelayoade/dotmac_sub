"""Preserve replay-safe Finance notification audiences for native Test Connections."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "646_test_connection_finance_review"
down_revision = "645_subscription_test_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_connection_finance_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_store.event_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "rule_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("automation_rule_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column(
            "service_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("recipient_ids", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "event_id",
            "rule_version_id",
            "step_index",
            name="uq_test_connection_review_step",
        ),
        sa.CheckConstraint("step_index >= 0", name="ck_test_connection_review_step"),
    )


def downgrade() -> None:
    # Do not erase a previously materialized notification audience.
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM test_connection_finance_reviews")
    ):
        raise RuntimeError("Finance review evidence exists; use a forward fix.")
    op.drop_table("test_connection_finance_reviews")
