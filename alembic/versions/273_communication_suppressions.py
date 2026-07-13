"""Platform-wide communication suppression ledger.

The single source of truth for whether we may contact an address on a channel.
Marketing eligibility previously lived inside the campaign segment filter, where
opting in was an optional checkbox and no unsubscribe ledger existed at all.

``scope`` is load-bearing: ``marketing`` blocks marketing only, ``all`` blocks
everything. An unsubscribe must never stop an invoice.

Revision ID: 273_communication_suppressions
Revises: 272_radius_nas_port_id_capacity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "273_communication_suppressions"
down_revision = "272_radius_nas_port_id_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "communication_suppressions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("raw_address", sa.String(length=320), nullable=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=True,
        ),
        sa.Column(
            "scope",
            sa.String(length=20),
            nullable=False,
            server_default="marketing",
        ),
        sa.Column(
            "reason",
            sa.String(length=20),
            nullable=False,
            server_default="unsubscribe",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.UniqueConstraint(
            "channel", "address", name="uq_communication_suppressions_channel_address"
        ),
    )
    op.create_index(
        "ix_communication_suppressions_channel",
        "communication_suppressions",
        ["channel"],
    )
    op.create_index(
        "ix_communication_suppressions_address",
        "communication_suppressions",
        ["address"],
    )
    op.create_index(
        "ix_communication_suppressions_subscriber_id",
        "communication_suppressions",
        ["subscriber_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_communication_suppressions_subscriber_id",
        table_name="communication_suppressions",
    )
    op.drop_index(
        "ix_communication_suppressions_address",
        table_name="communication_suppressions",
    )
    op.drop_index(
        "ix_communication_suppressions_channel",
        table_name="communication_suppressions",
    )
    op.drop_table("communication_suppressions")
