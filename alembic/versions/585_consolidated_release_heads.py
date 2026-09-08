"""Join the independently developed September 8 application migrations.

Revision ID: 585_consolidated_release_heads
Revises: the five 584 application branches

Each parent keeps its original upgrade and downgrade behavior. This revision
only joins their histories; it performs no schema or data mutation itself.
"""

revision: str = "585_consolidated_release_heads"
down_revision: tuple[str, ...] = (
    "584_customer_backed_quote_delivery",
    "584_quote_payment_review",
    "584_inbox_sla_rules",
    "584_field_request_requester_history",
    "584_team_inbox_queue_correctness",
)
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
