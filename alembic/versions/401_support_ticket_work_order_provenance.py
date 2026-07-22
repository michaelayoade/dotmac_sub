"""Backfill preserved CRM Ticket-to-Work-Order provenance.

Revision ID: 401_support_ticket_work_order_provenance
Revises: 400_subscription_relocation_intent

Migration 382 expanded the native ``origin_ticket_id`` relationship and moved
only corroborated legacy native UUID links. Imported CRM work orders correctly
retained ``crm_ticket_id``, but they could not yet be linked to an imported
native Ticket. The Ticket import preserves the external id in
``support_tickets.metadata.crm_ticket_id``; this migration uses that provenance
to backfill the native relationship while retaining the external identifier.

The verification gates fail closed on ambiguous CRM identities, subscriber
disagreement, or a conflicting pre-existing native link. Nothing is inferred
from title, tags, timestamps, or approximate text.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "401_support_ticket_work_order_provenance"
down_revision = "400_subscription_relocation_intent"
branch_labels = None
depends_on = None


def _count(bind, statement: str) -> int:
    return int(bind.execute(sa.text(statement)).scalar() or 0)


def _verify_unambiguous_ticket_provenance(bind) -> None:
    ambiguous = _count(
        bind,
        """
        SELECT count(*)
          FROM (
                SELECT metadata::jsonb ->> 'crm_ticket_id' AS crm_ticket_id
                  FROM support_tickets
                 WHERE metadata IS NOT NULL
                   AND NULLIF(metadata::jsonb ->> 'crm_ticket_id', '') IS NOT NULL
                 GROUP BY metadata::jsonb ->> 'crm_ticket_id'
                HAVING count(*) > 1
               ) AS duplicate_ticket_provenance
        """,
    )
    if ambiguous:
        raise RuntimeError(
            "CRM Ticket provenance is ambiguous across native Tickets; reconcile "
            f"{ambiguous} duplicated CRM identities before backfill"
        )


def _verify_candidate_alignment(bind) -> None:
    subscriber_mismatches = _count(
        bind,
        """
        SELECT count(*)
          FROM work_order AS wo
          JOIN support_tickets AS ticket
            ON wo.crm_ticket_id = ticket.metadata::jsonb ->> 'crm_ticket_id'
         WHERE wo.crm_ticket_id IS NOT NULL
           AND wo.origin_ticket_id IS NULL
           AND wo.subscriber_id IS DISTINCT FROM ticket.subscriber_id
        """,
    )
    if subscriber_mismatches:
        raise RuntimeError(
            "CRM Ticket-to-Work-Order provenance disagrees on subscriber identity; "
            f"reconcile {subscriber_mismatches} candidate links before backfill"
        )

    conflicting_links = _count(
        bind,
        """
        SELECT count(*)
          FROM work_order AS wo
          JOIN support_tickets AS ticket
            ON wo.crm_ticket_id = ticket.metadata::jsonb ->> 'crm_ticket_id'
         WHERE wo.crm_ticket_id IS NOT NULL
           AND wo.origin_ticket_id IS NOT NULL
           AND wo.origin_ticket_id IS DISTINCT FROM ticket.id
        """,
    )
    if conflicting_links:
        raise RuntimeError(
            "Existing native Work-Order origin conflicts with preserved CRM Ticket "
            f"provenance for {conflicting_links} rows"
        )


def upgrade() -> None:
    bind = op.get_bind()
    _verify_unambiguous_ticket_provenance(bind)
    _verify_candidate_alignment(bind)

    bind.execute(
        sa.text(
            """
            UPDATE work_order AS wo
               SET origin_ticket_id = ticket.id
              FROM support_tickets AS ticket
             WHERE wo.origin_ticket_id IS NULL
               AND wo.crm_ticket_id IS NOT NULL
               AND wo.crm_ticket_id =
                   ticket.metadata::jsonb ->> 'crm_ticket_id'
               AND wo.subscriber_id IS NOT DISTINCT FROM ticket.subscriber_id
            """
        )
    )

    _verify_candidate_alignment(bind)


def downgrade() -> None:
    raise RuntimeError(
        "CRM Ticket-to-Work-Order provenance cutover is irreversible"
    )
