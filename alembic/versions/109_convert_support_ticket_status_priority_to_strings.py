"""Convert support ticket status and priority columns to strings.

Revision ID: 109_convert_support_ticket_status_priority_to_strings
Revises: 108_drop_support_assignment_subscriber_fks
Create Date: 2026-05-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "109_convert_support_ticket_status_priority_to_strings"
down_revision = "108_drop_support_assignment_subscriber_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE support_tickets ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE support_tickets ALTER COLUMN priority DROP DEFAULT")
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN status TYPE VARCHAR(80) USING status::text"
    )
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN priority TYPE VARCHAR(40) USING priority::text"
    )
    op.execute("ALTER TABLE support_tickets ALTER COLUMN status SET DEFAULT 'open'")
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN priority SET DEFAULT 'normal'"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticketstatus') THEN
                CREATE TYPE ticketstatus AS ENUM (
                    'new',
                    'open',
                    'pending',
                    'waiting_on_customer',
                    'lastmile_rerun',
                    'site_under_construction',
                    'on_hold',
                    'resolved',
                    'closed',
                    'canceled',
                    'merged'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticketpriority') THEN
                CREATE TYPE ticketpriority AS ENUM (
                    'lower',
                    'low',
                    'medium',
                    'normal',
                    'high',
                    'urgent'
                );
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE support_tickets ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE support_tickets ALTER COLUMN priority DROP DEFAULT")
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN status TYPE ticketstatus USING status::ticketstatus"
    )
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN priority TYPE ticketpriority USING priority::ticketpriority"
    )
    op.execute("ALTER TABLE support_tickets ALTER COLUMN status SET DEFAULT 'open'")
    op.execute(
        "ALTER TABLE support_tickets ALTER COLUMN priority SET DEFAULT 'normal'"
    )
