"""Validate the latest RADIUS-session index on databases already stamped 408.

Revision ID: 410_validate_radius_session_latest_index
Revises: 409_tr069_operation_lifecycle

Revision 408 originally treated an index name as success.  PostgreSQL retains
that name after a failed concurrent build while marking the index invalid.
This forward guard repairs missing/interrupted builds and rejects a valid but
structurally different index before the release is considered migrated.
"""

from __future__ import annotations

from alembic import op
from scripts.migration.radius_session_latest_index import (
    INDEX_EXPRESSION,
    INDEX_NAME,
    TABLE_NAME,
    ensure_postgres_index,
)

revision = "410_validate_radius_session_latest_index"
down_revision = "409_tr069_operation_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            ensure_postgres_index(bind, op.execute)
    else:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            f"ON {TABLE_NAME} ({INDEX_EXPRESSION})"
        )


def downgrade() -> None:
    # Validation/repair revision: the index remains owned by revision 408.
    pass
