"""Cut over ticket-to-work-order provenance to a native one-to-many link.

Revision ID: 382_ticket_work_order_handoff
Revises: 380_integration_platform_cutover

The retired path treated a ``field_visit`` tag as an implicit issuance command,
stored one work-order id in ticket metadata, and reused the CRM provenance field
as a native ticket id. The native foreign key is authoritative after this
revision; the duplicated metadata and overloaded CRM values are removed.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "382_ticket_work_order_handoff"
down_revision = "381_operational_sla_policy_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_order",
        sa.Column("origin_ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_work_order_origin_ticket_id_support_tickets",
        "work_order",
        "support_tickets",
        ["origin_ticket_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_work_order_origin_ticket_id",
        "work_order",
        ["origin_ticket_id"],
    )

    # Backfill only rows created by the retired support-ticket automation.
    # Imported CRM provenance is deliberately left untouched.
    op.execute(
        sa.text(
            """
            UPDATE work_order AS wo
               SET origin_ticket_id = ticket.id,
                   crm_ticket_id = NULL
              FROM support_tickets AS ticket
             WHERE wo.origin_ticket_id IS NULL
               AND wo.crm_ticket_id = ticket.id::text
               AND (
                    ticket.metadata::jsonb ->> 'work_order_id' = wo.public_id
                    OR (
                        wo.metadata::jsonb ->> 'created_from' = 'support_ticket'
                        AND wo.metadata::jsonb ->> 'ticket_id' = ticket.id::text
                    )
               )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE support_tickets
               SET metadata = (metadata::jsonb - 'work_order_id')::json
             WHERE metadata IS NOT NULL
               AND metadata::jsonb ? 'work_order_id'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE work_order
               SET metadata = (((metadata::jsonb - 'ticket_id')
                                - 'ticket_number')
                                - 'created_from')::json
             WHERE origin_ticket_id IS NOT NULL
               AND metadata IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("ticket work-order authority cutover is irreversible")
