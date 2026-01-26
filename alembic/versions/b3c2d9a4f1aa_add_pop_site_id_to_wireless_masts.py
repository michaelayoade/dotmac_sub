"""add pop_site_id to wireless masts

Revision ID: b3c2d9a4f1aa
Revises: 4b1b2a5f0c6e
Create Date: 2026-01-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = "b3c2d9a4f1aa"
down_revision: Union[str, None] = "4b1b2a5f0c6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("wireless_masts")}
    if "pop_site_id" not in columns:
        op.add_column("wireless_masts", sa.Column("pop_site_id", UUID(as_uuid=True), nullable=True))

    indexes = {idx["name"] for idx in inspector.get_indexes("wireless_masts")}
    if "idx_wireless_masts_pop_site_id" not in indexes:
        op.create_index(
            "idx_wireless_masts_pop_site_id", "wireless_masts", ["pop_site_id"]
        )

    fks = {fk["name"] for fk in inspector.get_foreign_keys("wireless_masts")}
    if "fk_wireless_masts_pop_site_id" not in fks:
        op.create_foreign_key(
            "fk_wireless_masts_pop_site_id",
            "wireless_masts",
            "pop_sites",
            ["pop_site_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint("fk_wireless_masts_pop_site_id", "wireless_masts", type_="foreignkey")
    op.drop_index("idx_wireless_masts_pop_site_id", table_name="wireless_masts")
    op.drop_column("wireless_masts", "pop_site_id")
