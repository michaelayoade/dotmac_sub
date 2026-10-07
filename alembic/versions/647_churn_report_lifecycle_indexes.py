"""Add the churn report lifecycle-event access path.

The churn report filters trusted cancel/suspend evidence by event type and
effective time, correlates events back to subscriptions, and retains a
legacy subscriber fallback. Keep those bounded report indexes additive and
safe to retry on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "647_churn_report_lifecycle_indexes"
down_revision: str | None = "646_test_connection_finance_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    (
        "ix_subscription_lifecycle_events_churn_type_time",
        "subscription_lifecycle_events",
        "event_type, effective_at",
    ),
    (
        "ix_subscription_lifecycle_events_subscription_type_time",
        "subscription_lifecycle_events",
        "subscription_id, event_type, effective_at",
    ),
    (
        "ix_subscribers_churn_status_updated_at",
        "subscribers",
        "status, updated_at",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            try:
                for name, table, columns in _INDEXES:
                    op.execute(
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                        f"ON {table} ({columns})"
                    )
            finally:
                op.execute("RESET statement_timeout")
                op.execute("RESET lock_timeout")
        return
    for name, table, columns in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _table, _columns in reversed(_INDEXES):
                op.execute("DROP INDEX CONCURRENTLY IF EXISTS " + name)
        return
    for name, _table, _columns in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
