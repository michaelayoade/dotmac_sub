"""add user_type to subscribers

Revision ID: 7a1c9e2d4f55
Revises: 1c0efbd4db66
Create Date: 2026-02-23 10:05:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM


# revision identifiers, used by Alembic.
revision = "7a1c9e2d4f55"
down_revision = "1c0efbd4db66"
branch_labels = None
depends_on = None


USER_TYPE_VALUES = ("system_user", "customer", "reseller")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        user_type_enum = ENUM(*USER_TYPE_VALUES, name="usertype", create_type=False)
        enum_exists = bind.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
            {"name": "usertype"},
        ).fetchone()
        if enum_exists is None:
            user_type_enum.create(bind)

    if "subscribers" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("subscribers")}
        if "user_type" not in columns:
            op.add_column(
                "subscribers",
                sa.Column(
                    "user_type",
                    (
                        sa.Enum(*USER_TYPE_VALUES, name="usertype", create_type=False)
                        if is_postgres
                        else sa.String(length=20)
                    ),
                    nullable=False,
                    server_default="system_user",
                ),
            )

    op.execute(
        sa.text(
            "UPDATE subscribers SET user_type = 'system_user' WHERE user_type IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "subscribers" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("subscribers")}
        if "user_type" in columns:
            op.drop_column("subscribers", "user_type")

    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TYPE IF EXISTS usertype"))
