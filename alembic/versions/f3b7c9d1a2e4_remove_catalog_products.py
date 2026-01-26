"""Remove catalog products and offer product_id."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f3b7c9d1a2e4"
down_revision = "f2a9c1d4e7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "catalog_offers" in existing_tables:
        catalog_columns = {col["name"] for col in inspector.get_columns("catalog_offers")}
        if "product_id" in catalog_columns:
            op.drop_column("catalog_offers", "product_id")

    if "catalog_products" in existing_tables:
        op.drop_table("catalog_products")


def downgrade() -> None:
    op.create_table(
        "catalog_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("catalog_offers", sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_catalog_offers_product_id_catalog_products",
        "catalog_offers",
        "catalog_products",
        ["product_id"],
        ["id"],
    )
