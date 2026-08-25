"""Add Sub-owned bindings and roster detail for the composed inbox modules.

Workforce owns `service_teams`, `dotmac-inbox-operations` owns
`mod_inbox_ops.inbox_queues`, and this table is Sub's binding between them
(ADR-0013 § 7). It carries no foreign key to the queue: that row lives in
another schema owned by another distribution, and ADR-0011 keeps `public` and
`mod_*` free of cross-plane references.

Additive and reversible. It creates no queue and binds no team — populating it
is the backfill's job, and an empty table is the correct state until then.
The presence-detail table preserves `break` versus ordinary `away` while the
module owns the smaller dispatch-availability state.

Revision ID: 550_inbox_queue_bindings
Revises: 549_gateway_intent_lifecycle
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "550_inbox_queue_bindings"
down_revision: str | None = "549_gateway_intent_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbox_queue_bindings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "service_team_id",
            UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id"),
            nullable=False,
        ),
        sa.Column("queue_id", UUID(as_uuid=True), nullable=False),
        sa.Column("queue_code", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        # 1:1 in both directions. A team draining two queues, or two teams
        # draining one, is a policy change that must alter this constraint
        # deliberately rather than arriving as duplicate rows.
        sa.UniqueConstraint(
            "service_team_id", name="uq_inbox_queue_bindings_service_team"
        ),
        sa.UniqueConstraint("queue_id", name="uq_inbox_queue_bindings_queue"),
    )
    op.create_table(
        "inbox_agent_presence_details",
        sa.Column("person_id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("away_reason", sa.String(length=24), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "away_reason IS NULL OR away_reason IN ('away', 'break')",
            name="ck_inbox_agent_presence_details_away_reason",
        ),
    )


def downgrade() -> None:
    op.drop_table("inbox_agent_presence_details")
    op.drop_table("inbox_queue_bindings")
