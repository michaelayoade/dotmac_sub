"""Repair reporting indexes on databases already stamped past revision 658.

Preserve the same non-unique billing-period indexes and live-write-safe builds.
Bounded lock waits still apply; interrupted repairs are safe to retry. No data,
report semantics or migration history is rewritten. Downgrade retains indexes
owned by 658. See docs/runbooks/REGIONAL_REPORT_INDEX_RECOVERY.md.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from scripts.migration.regional_report_billing_indexes import ensure_postgres_indexes

revision: str = "662_validate_regional_report_billing_indexes"
down_revision: str | None = "661_support_ticket_comment_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            ensure_postgres_indexes(bind, op.execute)


def downgrade() -> None:
    # Verification/repair only. Revision 658 continues to own both indexes.
    pass
