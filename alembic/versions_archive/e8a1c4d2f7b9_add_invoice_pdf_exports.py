"""add invoice pdf exports

Revision ID: e8a1c4d2f7b9
Revises: b9c8d7e6f5a4
Create Date: 2026-02-22 12:55:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8a1c4d2f7b9"
down_revision: Union[str, None] = "b9c8d7e6f5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


invoicepdfexportstatus = sa.Enum(
    "queued",
    "processing",
    "completed",
    "failed",
    name="invoicepdfexportstatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Be resilient to partially-applied environments where the enum already exists.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                CREATE TYPE invoicepdfexportstatus AS ENUM ('queued', 'processing', 'completed', 'failed');
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )
    )

    if "invoice_pdf_exports" not in inspector.get_table_names():
        op.create_table(
            "invoice_pdf_exports",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "status",
                postgresql.ENUM(
                    "queued",
                    "processing",
                    "completed",
                    "failed",
                    name="invoicepdfexportstatus",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("celery_task_id", sa.String(length=120), nullable=True),
            sa.Column("file_path", sa.String(length=500), nullable=True),
            sa.Column("file_size_bytes", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
            sa.ForeignKeyConstraint(["requested_by_id"], ["subscribers.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("invoice_pdf_exports")}
    if "ix_invoice_pdf_exports_invoice_id" not in existing_indexes:
        op.create_index(
            "ix_invoice_pdf_exports_invoice_id",
            "invoice_pdf_exports",
            ["invoice_id"],
            unique=False,
        )
    if "ix_invoice_pdf_exports_status" not in existing_indexes:
        op.create_index(
            "ix_invoice_pdf_exports_status",
            "invoice_pdf_exports",
            ["status"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_invoice_pdf_exports_status", table_name="invoice_pdf_exports")
    op.drop_index("ix_invoice_pdf_exports_invoice_id", table_name="invoice_pdf_exports")
    op.drop_table("invoice_pdf_exports")
    invoicepdfexportstatus.drop(op.get_bind(), checkfirst=True)
