"""Merge the Inbox AI permission and operator-tenant migration heads.

Both branches were merged into ``dev`` from revision
``507_domain_settings_scope_columns``. This no-op revision preserves both
histories and restores one upgrade target without replaying either branch.

Revision ID: 510_merge_inbox_ai_and_operator_tenant_heads
Revises: 508_inbox_manager_ai_permission, 509_backfill_operator_tenant_scope
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "510_merge_inbox_ai_and_operator_tenant_heads"
down_revision: tuple[str, str] = (
    "508_inbox_manager_ai_permission",
    "509_backfill_operator_tenant_scope",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
