"""Drop SLA and workflow transition tables - CRM cleanup.

Revision ID: crm_cleanup_001
Revises: q4r5s6t7u8v9
Create Date: 2026-01-27 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "crm_cleanup_001"
down_revision: str = "q4r5s6t7u8v9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop SLA credit tables
    op.execute("DROP TABLE IF EXISTS sla_credit_items CASCADE")
    op.execute("DROP TABLE IF EXISTS sla_credit_reports CASCADE")

    # Drop SLA workflow tables
    op.execute("DROP TABLE IF EXISTS sla_breaches CASCADE")
    op.execute("DROP TABLE IF EXISTS sla_clocks CASCADE")
    op.execute("DROP TABLE IF EXISTS sla_targets CASCADE")
    op.execute("DROP TABLE IF EXISTS sla_policies CASCADE")

    # Drop workflow transition tables
    op.execute("DROP TABLE IF EXISTS ticket_status_transitions CASCADE")
    op.execute("DROP TABLE IF EXISTS work_order_status_transitions CASCADE")
    op.execute("DROP TABLE IF EXISTS project_task_status_transitions CASCADE")

    # Drop enum type if exists
    op.execute("DROP TYPE IF EXISTS workflowentitytype CASCADE")


def downgrade() -> None:
    # Tables cannot be restored without full schema recreation.
    # This is a destructive cleanup migration.
    pass
