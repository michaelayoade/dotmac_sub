"""Retire the VAS runtime without deleting financial history.

Revision ID: 300_retire_vas_runtime
Revises: 299_financial_access_consequence_evidence

The VAS tables are deliberately retained as an immutable archive. Cutover is
allowed only after every customer-liability wallet is zero and every external
money/delivery workflow is terminal. Active settings and schedules are then
removed so no stale adapter can resume writing after deployment.
"""

from __future__ import annotations

from collections.abc import Mapping

import sqlalchemy as sa

from alembic import op

revision = "300_retire_vas_runtime"
down_revision = "299_financial_access_consequence_evidence"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _count(bind, statement: str) -> int:
    return int(bind.execute(sa.text(statement)).scalar() or 0)


def _pending_vas_topups(bind) -> int:
    if not _has_table(bind, "topup_intents"):
        return 0
    table = sa.Table("topup_intents", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.select(table.c.status, table.c["metadata"]).where(
            table.c.status == "pending"
        )
    )
    pending = 0
    for status, metadata in rows:
        if str(status) != "pending" or not isinstance(metadata, Mapping):
            continue
        if metadata.get("payment_flow") == "vas_wallet_topup":
            pending += 1
    return pending


def _assert_safe_cutover(bind) -> None:
    blockers: dict[str, int] = {}
    if _has_table(bind, "vas_wallet_entries"):
        blockers["non_zero_wallets"] = _count(
            bind,
            """
            SELECT COUNT(*)
            FROM (
                SELECT wallet_id
                FROM vas_wallet_entries
                GROUP BY wallet_id
                HAVING ABS(SUM(
                    CASE WHEN CAST(entry_type AS TEXT) = 'credit'
                         THEN amount ELSE -amount END
                )) > 0.005
            ) AS non_zero_wallets
            """,
        )
    if _has_table(bind, "vas_transactions"):
        blockers["non_terminal_purchases"] = _count(
            bind,
            """
            SELECT COUNT(*) FROM vas_transactions
            WHERE CAST(status AS TEXT) IN ('pending', 'debited', 'submitted', 'review')
               OR (
                    CAST(status AS TEXT) = 'failed'
                    AND EXISTS (
                        SELECT 1 FROM vas_wallet_entries debit
                        WHERE debit.reference = 'vas-' || vas_transactions.request_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM vas_wallet_entries refund
                        WHERE refund.reference =
                              'vas-refund-' || vas_transactions.request_id
                    )
               )
            """,
        )
    if _has_table(bind, "vas_refund_requests"):
        blockers["non_terminal_refunds"] = _count(
            bind,
            """
            SELECT COUNT(*) FROM vas_refund_requests
            WHERE CAST(status AS TEXT) IN (
                'prepared', 'submitting', 'accepted', 'needs_attention'
            )
            """,
        )
    blockers["pending_gateway_topups"] = _pending_vas_topups(bind)
    blockers = {name: count for name, count in blockers.items() if count}
    if blockers:
        summary = ", ".join(f"{name}={count}" for name, count in blockers.items())
        raise RuntimeError(
            "VAS retirement blocked by unresolved customer money or provider "
            f"workflows: {summary}. Resolve them before deploying this revision."
        )


def _delete_retired_permissions(bind) -> None:
    if not _has_table(bind, "permissions"):
        return
    for assignment_table in (
        "role_permissions",
        "subscriber_permissions",
        "system_user_permissions",
    ):
        if _has_table(bind, assignment_table):
            bind.execute(
                sa.text(
                    f"DELETE FROM {assignment_table} WHERE permission_id IN ("
                    "SELECT id FROM permissions "
                    "WHERE key IN ('billing:vas:read', 'billing:vas:write'))"
                )
            )
    bind.execute(
        sa.text(
            "DELETE FROM permissions "
            "WHERE key IN ('billing:vas:read', 'billing:vas:write')"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_safe_cutover(bind)

    # Delivery tokens are no longer operationally useful and remain encrypted
    # secrets. Financial amounts, provider observations, and lifecycle rows stay.
    if _has_table(bind, "vas_transactions"):
        bind.execute(sa.text("UPDATE vas_transactions SET token_encrypted = NULL"))

    if _has_table(bind, "scheduled_tasks"):
        bind.execute(
            sa.text(
                "DELETE FROM scheduled_tasks "
                "WHERE task_name LIKE 'app.tasks.vas.%' OR name LIKE 'vas_%'"
            )
        )
    if _has_table(bind, "domain_settings"):
        bind.execute(
            sa.text("DELETE FROM domain_settings WHERE CAST(domain AS TEXT) = 'vas'")
        )
    _delete_retired_permissions(bind)


def downgrade() -> None:
    # Retirement removes configuration (including secret pointers) and cannot
    # safely reconstruct it. The archived evidence tables themselves are never
    # dropped, so a code rollback can explicitly re-seed reviewed configuration.
    pass
