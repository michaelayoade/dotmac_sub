"""Add released_at and release_reason to ont_assignments

Revision ID: 057_add_ont_assignment_release_fields
Revises: 056_drop_periodic_inform_server_default
Create Date: 2026-04-24

"""

import sqlalchemy as sa

from alembic import op

revision = "057_add_ont_assignment_release_fields"
down_revision = "056_drop_inform_server_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add released_at and release_reason columns to ont_assignments for audit
    # trail. These track when an assignment was closed and why
    # (e.g., "returned_to_inventory"). The squashed initial migration
    # builds the table from current models which already include these
    # columns, so guard with an existence check to keep this migration
    # idempotent against fresh-from-squash and pre-existing DBs alike.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("ont_assignments")}
    if "released_at" not in columns:
        op.add_column(
            "ont_assignments",
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "release_reason" not in columns:
        op.add_column(
            "ont_assignments",
            sa.Column("release_reason", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("ont_assignments", "release_reason")
    op.drop_column("ont_assignments", "released_at")
