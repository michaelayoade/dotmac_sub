"""Add business customer fields to subscribers.

Revision ID: c9d0e1f2a3b4
Revises: z7b8c9d0e1f2
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "z7b8c9d0e1f2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("subscribers")}

    additions = (
        ("company_name", sa.String(length=160)),
        ("legal_name", sa.String(length=200)),
        ("tax_id", sa.String(length=80)),
        ("domain", sa.String(length=120)),
        ("website", sa.String(length=255)),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column("subscribers", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("subscribers")}

    for name in ("website", "domain", "tax_id", "legal_name", "company_name"):
        if name in columns:
            op.drop_column("subscribers", name)
