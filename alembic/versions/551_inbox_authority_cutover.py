"""The sealed switch that moves inbox write authority to the composed modules.

ADR-0013 P5. The table holds at most one row, and its PRESENCE is the switch:
there is no `active` column to toggle and no delete path, because after the
module has written rows Sub never wrote, flipping back silently forks authority.

Additive and empty. Creating it activates nothing — `app/services/inbox_authority.py`
refuses to insert until the drift comparator is clean and a named review
reference is supplied.

Revision ID: 551_inbox_authority_cutover
Revises: 550_inbox_queue_bindings
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "551_inbox_authority_cutover"
down_revision: str | None = "550_inbox_queue_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbox_authority_cutovers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "singleton_key",
            sa.String(length=16),
            nullable=False,
            server_default="inbox",
        ),
        sa.Column("drift_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("conversations_verified", sa.Integer(), nullable=False),
        sa.Column("messages_verified", sa.Integer(), nullable=False),
        sa.Column("review_reference", sa.Text(), nullable=False),
        sa.Column("activated_by", sa.String(length=160), nullable=False),
        sa.Column("command_id", UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "cutover_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "singleton_key", name="uq_inbox_authority_cutover_singleton"
        ),
        # Belt and braces with the unique constraint above: the key cannot take
        # a second value, so the unique constraint cannot be sidestepped by
        # inserting a differently-keyed "second" cutover.
        sa.CheckConstraint(
            "singleton_key = 'inbox'",
            name="ck_inbox_authority_cutover_singleton_key",
        ),
        sa.CheckConstraint(
            "length(drift_fingerprint) = 64",
            name="ck_inbox_authority_cutover_fingerprint",
        ),
    )


def downgrade() -> None:
    """Reversible only while the table is EMPTY.

    Dropping a table that has been activated would erase the record that
    authority moved, while leaving every module row it authorised in place —
    the database would then have no answer to "who owns this conversation".
    Refusing is the only safe behaviour; the operator's route back is the
    reverse reconciler run in ADR-0013's rollback section.
    """
    activated = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM inbox_authority_cutovers")
    )
    if activated:
        raise RuntimeError(
            "inbox authority has been activated; dropping "
            "inbox_authority_cutovers would erase that record while leaving the "
            "module rows it authorised. See ADR-0013 'Rollback or forward-fix'."
        )
    op.drop_table("inbox_authority_cutovers")
