"""add crm message read_at

Revision ID: dc9b3d0b6b2a
Revises: 4b1b2a5f0c6e
Create Date: 2025-02-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "dc9b3d0b6b2a"
down_revision = "4b1b2a5f0c6e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("crm_messages")}
    if "read_at" not in columns:
        op.add_column(
            "crm_messages",
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("crm_messages", "read_at")
