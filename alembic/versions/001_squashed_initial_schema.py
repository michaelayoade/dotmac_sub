"""Squashed initial schema — all tables from models.

Creates the complete database schema directly from SQLAlchemy model
definitions. Replaces ~100+ individual migrations.

Revision ID: 001_squashed
Revises:
Create Date: 2026-03-29
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "001_squashed"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Create required extensions
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis_topology"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    conn.execute(text("COMMIT"))

    # Import all models so Base.metadata knows about them
    import app.models  # noqa: F401
    from app.db import Base

    # Create all tables from the model definitions
    Base.metadata.create_all(conn.engine)


def downgrade() -> None:
    raise RuntimeError(
        "Cannot downgrade squashed initial migration. "
        "Restore from backup instead."
    )
