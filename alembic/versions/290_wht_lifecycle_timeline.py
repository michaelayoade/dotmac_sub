"""Add the owned WHT lifecycle and append-only transition timeline.

Revision ID: 290_wht_lifecycle
Revises: 289_merge_support_subscription_and_firmware_heads
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "290_wht_lifecycle"
down_revision = "289_merge_support_subscription_and_firmware_heads"
branch_labels = None
depends_on = None

_WHT_STATUS = postgresql.ENUM(
    "pending",
    "certified",
    "reclaimed",
    "written_off",
    name="withholdingtaxstatus",
    create_type=False,
)


def upgrade() -> None:
    op.create_index(
        "uq_withholding_tax_records_payment_id",
        "withholding_tax_records",
        ["payment_id"],
        unique=True,
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("certificate_reference", sa.String(160)),
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("certified_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "withholding_tax_records",
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "withholding_tax_transitions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("withholding_tax_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", _WHT_STATUS),
        sa.Column("to_status", _WHT_STATUS, nullable=False),
        sa.Column("actor_id", sa.String(120)),
        sa.Column("certificate_reference", sa.String(160)),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status <> to_status",
            name="ck_withholding_tax_transitions_status_change",
        ),
    )
    op.create_index(
        "ix_withholding_tax_transitions_record_occurred",
        "withholding_tax_transitions",
        ["record_id", "occurred_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_wht_transition_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION
                    'withholding_tax_transitions is append-only'
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER withholding_tax_transitions_append_only
            BEFORE UPDATE OR DELETE ON withholding_tax_transitions
            FOR EACH ROW EXECUTE FUNCTION reject_wht_transition_mutation()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER withholding_tax_transitions_append_only "
            "ON withholding_tax_transitions"
        )
        op.execute("DROP FUNCTION reject_wht_transition_mutation()")

    op.drop_index(
        "ix_withholding_tax_transitions_record_occurred",
        table_name="withholding_tax_transitions",
    )
    op.drop_table("withholding_tax_transitions")
    op.drop_index(
        "uq_withholding_tax_records_payment_id",
        table_name="withholding_tax_records",
    )
    op.drop_column("withholding_tax_records", "resolved_at")
    op.drop_column("withholding_tax_records", "certified_at")
    op.drop_column("withholding_tax_records", "certificate_reference")
