"""Merge the inbox AI permission head with the operator-tenant head.

``507_domain_settings_scope_columns`` forked: #2203 added
``508_inbox_manager_ai_permission`` directly on top of it while
``508_operator_tenant_tables`` -> ``509_backfill_operator_tenant_scope``
already descended from it. Two heads make ``alembic upgrade head`` ambiguous,
so the PostgreSQL gate fails on every pull request, including ones that add no
migration of their own.

No schema change: the two lineages touch different tables and neither depends
on the other.

Revision ID: 510_merge_inbox_ai_and_operator_tenant_heads
Revises: 508_inbox_manager_ai_permission, 509_backfill_operator_tenant_scope
"""

from __future__ import annotations

revision = "510_merge_inbox_ai_and_operator_tenant_heads"
down_revision = (
    "508_inbox_manager_ai_permission",
    "509_backfill_operator_tenant_scope",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
