"""Join purchase history with prepaid sweep metrics without changing behavior."""

from __future__ import annotations

revision: str = "657_purchase_prepaid_sweep_merge"
down_revision: tuple[str, str] = (
    "655_prepaid_purchase_current_main_merge",
    "651_prepaid_sweep_cycle_totals",
)
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Apply both existing histories and record their common head."""


def downgrade() -> None:
    """Restore both parent markers without altering data or feature policy."""
