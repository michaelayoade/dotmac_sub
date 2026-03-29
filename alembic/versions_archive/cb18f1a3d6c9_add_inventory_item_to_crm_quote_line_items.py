"""Link CRM quote line items to inventory items."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "cb18f1a3d6c9"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("crm_quote_line_items")}
    if "inventory_item_id" not in columns:
        op.add_column(
            "crm_quote_line_items",
            sa.Column("inventory_item_id", UUID(as_uuid=True), nullable=True),
        )
    fks = {fk["name"] for fk in inspector.get_foreign_keys("crm_quote_line_items")}
    if "fk_crm_quote_line_items_inventory_item_id" not in fks:
        op.create_foreign_key(
            "fk_crm_quote_line_items_inventory_item_id",
            "crm_quote_line_items",
            "inventory_items",
            ["inventory_item_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint(
        "fk_crm_quote_line_items_inventory_item_id",
        "crm_quote_line_items",
        type_="foreignkey",
    )
    op.drop_column("crm_quote_line_items", "inventory_item_id")
