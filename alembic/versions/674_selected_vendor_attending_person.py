"""Explicit staff-selected native attending vendor membership; no inferred backfill."""

import sqlalchemy as sa

from alembic import op

revision: str = "674_selected_vendor_attending_person"
down_revision: str | None = "673_explicit_work_order_classification"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.add_column(
        "work_order_assignment_queue",
        sa.Column("assigned_vendor_user_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_work_order_selected_vendor_user",
        "work_order_assignment_queue",
        "field_vendor_users",
        ["assigned_vendor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_work_order_selected_vendor_user",
        "work_order_assignment_queue",
        "assigned_vendor_user_id IS NULL OR (assigned_vendor_id IS NOT NULL AND assigned_technician_id IS NULL)",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Selected attending vendor identity requires a forward fix; preserve schema and history"
    )
