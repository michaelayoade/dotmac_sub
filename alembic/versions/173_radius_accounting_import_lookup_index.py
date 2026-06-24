"""Add radius accounting importer lookup index.

Revision ID: 173_radius_accounting_import_lookup_index
Revises: 172_splynx_usage_history_import
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op

revision = "173_radius_accounting_import_lookup_index"
down_revision = "172_splynx_usage_history_import"
branch_labels = None
depends_on = None

_INDEX = "ix_radius_accounting_sessions_credential_session"
_TABLE = "radius_accounting_sessions"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON {_TABLE} (access_credential_id, session_id)"
            )
    else:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} (access_credential_id, session_id)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    else:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
