"""Add fiber change requests for vendor edits.

Revision ID: 1f2a3c4d5e6f
Revises: e3f1a8b2c4d6
Create Date: 2026-01-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "1f2a3c4d5e6f"
down_revision = "e3f1a8b2c4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status_enum = postgresql.ENUM(
        "pending",
        "applied",
        "rejected",
        name="fiberchangerequeststatus",
    )
    operation_enum = postgresql.ENUM(
        "create",
        "update",
        "delete",
        name="fiberchangerequestoperation",
    )
    status_enum.create(op.get_bind(), checkfirst=True)
    operation_enum.create(op.get_bind(), checkfirst=True)

    status_enum = postgresql.ENUM(
        "pending",
        "applied",
        "rejected",
        name="fiberchangerequeststatus",
        create_type=False,
    )
    operation_enum = postgresql.ENUM(
        "create",
        "update",
        "delete",
        name="fiberchangerequestoperation",
        create_type=False,
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "fiber_change_requests" not in existing_tables:
        op.create_table(
            "fiber_change_requests",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("asset_type", sa.String(80), nullable=False),
            sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("operation", operation_enum, nullable=False),
            sa.Column("payload", postgresql.JSON(), nullable=False),
            sa.Column("status", status_enum, nullable=False, server_default="pending"),
            sa.Column("requested_by_person_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("people.id"), nullable=True),
            sa.Column("requested_by_vendor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vendors.id"), nullable=True),
            sa.Column("reviewed_by_person_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("people.id"), nullable=True),
            sa.Column("review_notes", sa.Text(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    indexes = {idx["name"] for idx in inspector.get_indexes("fiber_change_requests")} if "fiber_change_requests" in existing_tables else set()
    if "ix_fiber_change_requests_status" not in indexes:
        op.create_index("ix_fiber_change_requests_status", "fiber_change_requests", ["status"])
    if "ix_fiber_change_requests_asset_type" not in indexes:
        op.create_index("ix_fiber_change_requests_asset_type", "fiber_change_requests", ["asset_type"])


def downgrade() -> None:
    op.drop_index("ix_fiber_change_requests_asset_type", table_name="fiber_change_requests")
    op.drop_index("ix_fiber_change_requests_status", table_name="fiber_change_requests")
    op.drop_table("fiber_change_requests")
    op.execute("DROP TYPE fiberchangerequestoperation")
    op.execute("DROP TYPE fiberchangerequeststatus")
