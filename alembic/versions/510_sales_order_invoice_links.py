"""Structural SalesOrder <-> Invoice link, replacing the metadata join.

Sale-to-money was joined through ``Project.metadata_``: a JSON string
comparison on ``sales_order_id`` plus ``selfcare_installation_invoice_id``,
with a full-scan fallback. Settlement evidence therefore could not be
attributed to the commercial order, and nothing could derive the order's
financial status from it.

The table lives in Sales. ``invoices`` gains no ``sales_order_id`` column, so
Finance keeps owning settlement without depending on Sales.

Expand only. The historical metadata keys are left in place and are still the
read path; this migration backfills what they already assert so a parity check
can compare the two before any read cutover.

Revision ID: 510_sales_order_invoice_links
Revises: 509_backfill_operator_tenant_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "510_sales_order_invoice_links"
down_revision = "509_backfill_operator_tenant_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_order_invoice_links",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "sales_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column(
            "origin", sa.String(length=16), nullable=False, server_default="native"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "invoice_id", name="uq_sales_order_invoice_links_invoice_id"
        ),
        sa.CheckConstraint(
            "purpose IN ('installation', 'deposit', 'subscription')",
            name="ck_sales_order_invoice_links_purpose",
        ),
        sa.CheckConstraint(
            "origin IN ('native', 'backfill')",
            name="ck_sales_order_invoice_links_origin",
        ),
    )
    op.create_index(
        "ix_sales_order_invoice_links_sales_order_id",
        "sales_order_invoice_links",
        ["sales_order_id"],
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # The backfill reads a JSON path and relies on ON CONFLICT; the SQLite
        # metadata lane has no rows to recover.
        return

    # Recover what the metadata join already asserts. Only rows whose invoice,
    # sales order and account all still resolve are linked — a dangling id is
    # left for review rather than forced through a RESTRICT foreign key.
    # DISTINCT ON keeps one row per invoice, honouring the unique constraint
    # when two projects name the same invoice.
    op.execute(
        sa.text(
            """
            INSERT INTO sales_order_invoice_links
                (id, sales_order_id, invoice_id, account_id, purpose, origin, created_at)
            SELECT DISTINCT ON (i.id)
                gen_random_uuid(),
                so.id,
                i.id,
                so.subscriber_id,
                'installation',
                'backfill',
                now()
            FROM projects p
            JOIN sales_orders so
              ON so.id = (p.metadata_ ->> 'sales_order_id')::uuid
            JOIN invoices i
              ON i.id = (p.metadata_ ->> 'selfcare_installation_invoice_id')::uuid
            JOIN subscribers s
              ON s.id = so.subscriber_id
            WHERE p.metadata_ ->> 'sales_order_id' IS NOT NULL
              AND p.metadata_ ->> 'selfcare_installation_invoice_id' IS NOT NULL
              AND p.metadata_ ->> 'sales_order_id' ~
                  '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
              AND p.metadata_ ->> 'selfcare_installation_invoice_id' ~
                  '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            ORDER BY i.id, p.created_at DESC
            ON CONFLICT ON CONSTRAINT uq_sales_order_invoice_links_invoice_id
            DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sales_order_invoice_links_sales_order_id",
        table_name="sales_order_invoice_links",
    )
    op.drop_table("sales_order_invoice_links")
