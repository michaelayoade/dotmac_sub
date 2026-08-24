"""Store merged support-ticket sources as canceled tickets.

Merge is a relation-backed disposition, not a lifecycle status. Existing
``merged`` Ticket rows are changed to ``canceled`` while their
``merged_into_ticket_id`` relation and historical fields remain intact.
Where a legacy row lost that pointer but still has TicketMerge evidence, the
latest merge target repairs the pointer before the status backfill.

The former merge implementation copied ticket attachment metadata to the
target but left its private StoredFile ownership and source metadata behind.
Those exact file rows are rebound to the recorded target and the source's
already-copied attachment list is cleared.

The retired value is also removed from operator-configured status choices.
Automation rules that mention it are disabled rather than reinterpreted as an
ordinary cancellation, which would broaden their meaning.

Every statement is predicate-bounded and safe to rerun. PostgreSQL lock and
statement timeouts keep deployment failure bounded. Downgrade is a no-op:
restoring a fake lifecycle status would discard the new status/relation model.

Revision ID: 551_cancel_merged_ticket_sources
Revises: 550_integrator_provider_ref
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "551_cancel_merged_ticket_sources"
down_revision: str | None = "550_integrator_provider_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        bind.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

        bind.execute(
            sa.text(
                """
                WITH latest_merge AS (
                    SELECT DISTINCT ON (source_ticket_id)
                           source_ticket_id,
                           target_ticket_id
                      FROM support_ticket_merges
                     ORDER BY source_ticket_id, created_at DESC, target_ticket_id DESC
                )
                UPDATE support_tickets AS ticket
                   SET merged_into_ticket_id = latest_merge.target_ticket_id
                  FROM latest_merge
                 WHERE ticket.id = latest_merge.source_ticket_id
                   AND ticket.status = 'merged'
                   AND ticket.merged_into_ticket_id IS NULL
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE stored_files AS file
                   SET entity_id = ticket.merged_into_ticket_id::text
                  FROM support_tickets AS ticket
                 WHERE ticket.status = 'merged'
                   AND ticket.merged_into_ticket_id IS NOT NULL
                   AND file.entity_id = ticket.id::text
                   AND file.entity_type IN (
                       'support_ticket_attachment',
                       'support_ticket_comment_attachment'
                   )
                """
            )
        )

    bind.execute(
        sa.text(
            """
            UPDATE support_tickets
               SET status = 'canceled',
                   attachments = '[]'
             WHERE status = 'merged'
            """
        )
    )

    if bind.dialect.name != "postgresql":
        return

    bind.execute(
        sa.text(
            """
            UPDATE domain_settings
               SET value_json = (value_json::jsonb - 'merged')::json
             WHERE key = 'support_ticket_status_options'
               AND jsonb_typeof(value_json::jsonb) = 'array'
               AND value_json::jsonb ? 'merged'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE support_ticket_automation_rules
               SET is_active = false
             WHERE is_active = true
               AND (
                    (action_type::text = 'set_status'
                     AND action_value->>'status' = 'merged')
                    OR conditions->>'status' = 'merged'
               )
            """
        )
    )


def downgrade() -> None:
    """Do not recreate the retired lifecycle status or falsify history."""
