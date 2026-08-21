"""Add Team Inbox observation semantic fingerprints and quarantine.

Revision ID: 548_team_inbox_observation_quarantine
Revises: 547_inbox_thread_continuation
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "548_team_inbox_observation_quarantine"
down_revision: str | None = "547_inbox_thread_continuation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbox_provider_observations",
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "inbox_provider_observations",
        sa.Column("semantic_fingerprint_version", sa.Integer(), nullable=True),
    )
    op.create_table(
        "inbox_provider_observation_collisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "candidate_payload_fingerprint", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "candidate_semantic_fingerprint", sa.String(length=64), nullable=False
        ),
        sa.Column("semantic_fingerprint_version", sa.Integer(), nullable=False),
        sa.Column("candidate_evidence", sa.JSON(), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=40),
            server_default="quarantined",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["inbox_provider_observations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id",
            "candidate_semantic_fingerprint",
            name="uq_inbox_observation_collision_semantic",
        ),
    )
    op.create_index(
        "ix_inbox_observation_collisions_status",
        "inbox_provider_observation_collisions",
        ["status", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_observation_collisions_status",
        table_name="inbox_provider_observation_collisions",
    )
    op.drop_table("inbox_provider_observation_collisions")
    op.drop_column("inbox_provider_observations", "semantic_fingerprint_version")
    op.drop_column("inbox_provider_observations", "semantic_fingerprint")
