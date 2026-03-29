"""Remove speed tiers and references."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f2a9c1d4e7b8"
down_revision = "f1d8a2c3b4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "offer_versions" in existing_tables:
        offer_columns = {col["name"] for col in inspector.get_columns("offer_versions")}
        if "speed_tier_id" in offer_columns:
            op.drop_column("offer_versions", "speed_tier_id")

    if "catalog_offers" in existing_tables:
        catalog_columns = {col["name"] for col in inspector.get_columns("catalog_offers")}
        if "speed_tier_id" in catalog_columns:
            op.drop_column("catalog_offers", "speed_tier_id")

    if "speed_tiers" in existing_tables:
        op.drop_table("speed_tiers")


def downgrade() -> None:
    op.create_table(
        "speed_tiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("down_mbps", sa.Integer(), nullable=False),
        sa.Column("up_mbps", sa.Integer(), nullable=False),
        sa.Column("qos_class", sa.String(length=80), nullable=True),
        sa.Column("is_business_grade", sa.Boolean(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("catalog_offers", sa.Column("speed_tier_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("offer_versions", sa.Column("speed_tier_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_catalog_offers_speed_tier_id_speed_tiers",
        "catalog_offers",
        "speed_tiers",
        ["speed_tier_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_offer_versions_speed_tier_id_speed_tiers",
        "offer_versions",
        "speed_tiers",
        ["speed_tier_id"],
        ["id"],
    )
