"""Join field lifecycle and customer connection type migration paths.

Both parent revisions have already applied their own schema changes. This
revision only joins their histories; it performs no DDL and rewrites no data.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "683_field_lifecycle_connection_types_merge"
down_revision: tuple[str, str] = (
    "682_accepted_project_field_completion",
    "671_customer_connection_type_definitions",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge histories after both upgrade paths have completed."""


def downgrade() -> None:
    """Separate the histories without changing schema or data."""
