"""Per-subscriber snooze state for the location-confirmation prompt.

Revision ID: 348_location_capture_prompt_state
Revises: 347_work_order_evidence_drop_crm_id
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "348_location_capture_prompt_state"
down_revision: str | None = "347_work_order_evidence_drop_crm_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "location_capture_prompt_states",
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_prompted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismiss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("location_capture_prompt_states")
