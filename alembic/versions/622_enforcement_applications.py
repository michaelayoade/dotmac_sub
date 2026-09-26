"""Add enforcement_applications: durable per-NAS enforcement evidence.

ADR-0017: a new observation table recording the outcome of every subscription
enforcement attempt (address-list block/unblock, session kick) against a NAS
device. Additive only; no existing column changes. No foreign keys are
declared on ``subscription_id``/``nas_device_id`` on purpose — the sole writer
(``access.session_enforcement``) opens its evidence write on an independent
connection while the calling transaction may hold ``SELECT ... FOR UPDATE`` on
the subscription row, and an FK check from a second connection would
self-deadlock against that lock.

Revision ID: 622_enforcement_applications
Revises: 621_ont_assignment_mode_normalization
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "622_enforcement_applications"
down_revision: str | None = "621_ont_assignment_mode_normalization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enforcement_applications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nas_device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effect", sa.String(length=30), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("failure_class", sa.String(length=20), nullable=True),
        sa.Column("path", sa.String(length=10), nullable=True),
        sa.Column("detail", sa.String(length=2000), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effect IN ('address_list_block', 'address_list_unblock', 'session_kick')",
            name="ck_enforcement_applications_effect",
        ),
        sa.CheckConstraint(
            "outcome IN ('applied', 'failed', 'not_applicable')",
            name="ck_enforcement_applications_outcome",
        ),
        sa.CheckConstraint(
            "failure_class IS NULL OR failure_class IN "
            "('auth_rejected', 'unreachable', 'timeout', 'command_failed', "
            "'not_capable')",
            name="ck_enforcement_applications_failure_class",
        ),
        sa.CheckConstraint(
            "path IS NULL OR path IN ('ssh', 'api')",
            name="ck_enforcement_applications_path",
        ),
        sa.CheckConstraint(
            "outcome != 'not_applicable' OR failure_class IS NULL",
            name="ck_enforcement_applications_not_applicable_no_failure_class",
        ),
        sa.CheckConstraint(
            "outcome != 'failed' OR failure_class IS NOT NULL",
            name="ck_enforcement_applications_failed_has_failure_class",
        ),
        sa.UniqueConstraint(
            "subscription_id",
            "nas_device_id",
            "effect",
            name="uq_enforcement_applications_subscription_nas_effect",
        ),
    )
    op.create_index(
        "ix_enforcement_applications_outcome_failure_class",
        "enforcement_applications",
        ["outcome", "failure_class"],
    )
    op.create_index(
        "ix_enforcement_applications_nas_device",
        "enforcement_applications",
        ["nas_device_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_enforcement_applications_nas_device",
        table_name="enforcement_applications",
    )
    op.drop_index(
        "ix_enforcement_applications_outcome_failure_class",
        table_name="enforcement_applications",
    )
    op.drop_table("enforcement_applications")
