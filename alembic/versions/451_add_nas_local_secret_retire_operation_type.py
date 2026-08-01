"""add nas local secret retire operation type

Retiring a shadowing NAS-local PPPoE secret is a tracked device operation, not a
log line: it needs idempotency, retry and durable failure evidence. See
docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md.

Revision ID: 451_add_nas_local_secret_retire_operation_type
Revises: 450_fiber_test_acceptance
Create Date: 2026-08-01
"""

from alembic import op

revision = "451_add_nas_local_secret_retire_operation_type"
down_revision = "450_fiber_test_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE networkoperationtype "
        "ADD VALUE IF NOT EXISTS 'nas_local_secret_retire'"
    )


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value in place. Retirement rows are
    # historical device evidence, so they are retained rather than rewritten to
    # an unrelated type; the value is simply left present.
    pass
