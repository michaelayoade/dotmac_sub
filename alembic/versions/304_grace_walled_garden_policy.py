"""Persist the effective financial access restriction tier.

Revision ID: 304_grace_walled_garden_policy
Revises: 303_payment_import_batch_reversal
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "304_grace_walled_garden_policy"
down_revision = "303_payment_import_batch_reversal"
branch_labels = None
depends_on = None

_access_mode = postgresql.ENUM(
    "hard_reject",
    "captive",
    name="accessrestrictionmode",
    create_type=False,
)


def upgrade() -> None:
    _access_mode.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "enforcement_locks",
        sa.Column(
            "access_mode",
            _access_mode,
            nullable=False,
            server_default="hard_reject",
        ),
    )
    op.add_column(
        "financial_access_consequences",
        sa.Column("access_mode", _access_mode, nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE domain_settings
            SET is_active = false
            WHERE domain = 'collections'
              AND key IN ('prepaid_grace_days', 'prepaid_deactivation_days')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE policy_sets
            SET name = 'Default — Postpaid (steps after effective grace)'
            WHERE id = CAST('0d000000-0000-4000-8000-00000000d002' AS uuid)
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE policy_sets
            SET name = 'Default — Postpaid (suspend at 60 days)'
            WHERE id = CAST('0d000000-0000-4000-8000-00000000d002' AS uuid)
            """
        )
    )
    op.drop_column("financial_access_consequences", "access_mode")
    op.drop_column("enforcement_locks", "access_mode")
    _access_mode.drop(op.get_bind(), checkfirst=True)
