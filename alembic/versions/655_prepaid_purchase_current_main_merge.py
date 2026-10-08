"""Join approved purchase history with current main without rewriting revisions."""

from __future__ import annotations

revision: str = "655_prepaid_purchase_current_main_merge"
down_revision: tuple[str, str] = (
    "647_purchase_outage_approval",
    "650_customer_connection_type",
)
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Record both already-applied native histories as one effective head."""


def downgrade() -> None:
    """Restore the parent head markers without changing either schema."""
