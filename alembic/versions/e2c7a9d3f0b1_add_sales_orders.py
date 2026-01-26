"""Add sales orders and line items."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, UUID

# revision identifiers, used by Alembic.
revision = "e2c7a9d3f0b1"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE salesorderstatus AS ENUM ('draft', 'confirmed', 'paid', 'fulfilled', 'cancelled');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE salesorderpaymentstatus AS ENUM ('pending', 'partial', 'paid', 'waived');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    op.create_table(
        "sales_orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("quote_id", UUID(as_uuid=True), sa.ForeignKey("crm_quotes.id")),
        sa.Column("person_id", UUID(as_uuid=True), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("subscriber_accounts.id")),
        sa.Column("invoice_id", UUID(as_uuid=True), sa.ForeignKey("invoices.id")),
        sa.Column("service_order_id", UUID(as_uuid=True), sa.ForeignKey("service_orders.id")),
        sa.Column("order_number", sa.String(80)),
        sa.Column(
            "status",
            ENUM(
                "draft",
                "confirmed",
                "paid",
                "fulfilled",
                "cancelled",
                name="salesorderstatus",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "payment_status",
            ENUM(
                "pending",
                "partial",
                "paid",
                "waived",
                name="salesorderpaymentstatus",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount_paid", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("balance_due", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("payment_due_date", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("deposit_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deposit_paid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("contract_signed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("signed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("metadata", sa.JSON),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_number", name="uq_sales_orders_order_number"),
        sa.UniqueConstraint("quote_id", name="uq_sales_orders_quote_id"),
    )
    op.create_index("ix_sales_orders_person_id", "sales_orders", ["person_id"])
    op.create_index("ix_sales_orders_account_id", "sales_orders", ["account_id"])
    op.create_index("ix_sales_orders_status", "sales_orders", ["status"])
    op.create_index("ix_sales_orders_payment_status", "sales_orders", ["payment_status"])

    op.create_table(
        "sales_order_lines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sales_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sales_orders.id"),
            nullable=False,
        ),
        sa.Column("inventory_item_id", UUID(as_uuid=True), sa.ForeignKey("inventory_items.id")),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1.000"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sales_order_lines_sales_order_id", "sales_order_lines", ["sales_order_id"])


def downgrade() -> None:
    op.drop_index("ix_sales_order_lines_sales_order_id", table_name="sales_order_lines")
    op.drop_table("sales_order_lines")
    op.drop_index("ix_sales_orders_payment_status", table_name="sales_orders")
    op.drop_index("ix_sales_orders_status", table_name="sales_orders")
    op.drop_index("ix_sales_orders_account_id", table_name="sales_orders")
    op.drop_index("ix_sales_orders_person_id", table_name="sales_orders")
    op.drop_table("sales_orders")
    op.execute("DROP TYPE IF EXISTS salesorderpaymentstatus")
    op.execute("DROP TYPE IF EXISTS salesorderstatus")
