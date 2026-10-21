"""Join preserved field lifecycle and published router-health histories.

Both parent revisions retain their schema changes. This revision only joins
migration histories and performs no schema or data changes.
"""

from __future__ import annotations

revision: str = "686_field_lifecycle_router_health_merge"
down_revision: tuple[str, str] = (
    "684_customer_connection_type_defaults",
    "672_router_health_reconciler",
)
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Join histories without schema or data changes."""


def downgrade() -> None:
    """Separate histories without schema or data changes."""
