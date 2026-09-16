"""Store exact, bounded account-recovery command replay evidence.

Revision ID: 614_recovery_blocked_refs
Revises: 613_account_recovery_permissions
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "614_recovery_blocked_refs"
down_revision: str | None = "613_account_recovery_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_recovery_blocked_preflight",
        sa.Column(
            "idempotency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("idempotency_keys.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "blocked_subscription_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
        ),
        sa.Column(
            "unsupported_consequences",
            postgresql.ARRAY(sa.String(length=48)),
            nullable=False,
        ),
    )
    op.create_table(
        "account_recovery_command_outcomes",
        sa.Column(
            "idempotency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("idempotency_keys.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account_recovery_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("generation", sa.Integer()),
        sa.Column(
            "affected_subscription_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
        ),
        sa.Column(
            "restored_subscription_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
        ),
        sa.Column(
            "unrestored_subscription_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
        ),
        sa.Column(
            "drifted_subscription_ids",
            postgresql.ARRAY(sa.String(length=36)),
            nullable=False,
        ),
        sa.Column(
            "missing_participant_types",
            postgresql.ARRAY(sa.String(length=48)),
            nullable=False,
        ),
        sa.Column("confirmation_fingerprint", sa.String(length=64)),
        sa.Column("fingerprint_revision", sa.Integer()),
        sa.Column(
            "affected_resource_types",
            postgresql.ARRAY(sa.String(length=48)),
            nullable=False,
        ),
    )


def downgrade() -> None:
    outcomes = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM account_recovery_command_outcomes"))
        .scalar_one()
    )
    if outcomes:
        raise RuntimeError(
            "Refusing to downgrade 614_recovery_blocked_refs: "
            "durable command outcome evidence exists"
        )
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM account_recovery_blocked_preflight"))
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Refusing to downgrade 614_recovery_blocked_refs: "
            "durable preflight replay evidence exists"
        )
    op.drop_table("account_recovery_command_outcomes")
    op.drop_table("account_recovery_blocked_preflight")
