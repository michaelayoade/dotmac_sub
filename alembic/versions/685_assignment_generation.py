"""Bind field observations to an explicit assignment generation.

Revision ID: 671_assignment_generation
Revises: 670_user_credential_version

Expand only: historical assignments and observations retain NULL provenance.
New explicit assignments mint a UUID. Schedule-only commands preserve it.
No historical observation is inferred to belong to a new assignment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "671_assignment_generation"
down_revision: str | None = "670_user_credential_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in (
        "work_order_assignment_queue",
        "field_job_events",
        "field_tech_location_pings",
    ):
        op.add_column(
            table,
            sa.Column(
                "assignment_generation", postgresql.UUID(as_uuid=True), nullable=True
            ),
        )
    op.add_column(
        "work_order_assignment_queue",
        sa.Column("assignment_effective_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("work_order_assignment_queue", "assignment_effective_at")
    for table in (
        "field_tech_location_pings",
        "field_job_events",
        "work_order_assignment_queue",
    ):
        op.drop_column(table, "assignment_generation")
