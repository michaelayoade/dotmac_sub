"""Add access_state column to subscriptions.

Phase 2 of the RADIUS access-state refactor.
See docs/radius_state_refactor/phase0_state_model.md.

The column is nullable so existing rows are unaffected. Backfill is
deferred to phase 5. Nothing in app code reads it yet (phase 3 starts
shadow-writing).

Revision ID: 114_add_subscription_access_state
Revises: 113_add_catalog_offer_plan_family
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "114_add_subscription_access_state"
down_revision = "113_add_catalog_offer_plan_family"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("subscriptions")}
    if "access_state" not in existing:
        op.add_column(
            "subscriptions",
            sa.Column("access_state", sa.String(length=20), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("subscriptions")}
    if "access_state" in existing:
        op.drop_column("subscriptions", "access_state")
