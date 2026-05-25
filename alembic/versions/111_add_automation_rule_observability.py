"""Add observability columns to support_ticket_automation_rules.

Adds last_fired_at, last_error, last_error_at so operators can see whether
a rule has actually run and why a misconfigured rule isn't applying.

Revision ID: 111_add_automation_rule_observability
Revises: 110_merge_heads_and_add_ipam_indexes
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "111_add_automation_rule_observability"
down_revision = "110_merge_heads_and_add_ipam_indexes"
branch_labels = None
depends_on = None


TABLE_NAME = "support_ticket_automation_rules"
COLUMNS = (
    ("last_fired_at", sa.DateTime(timezone=True), True),
    ("last_error", sa.Text(), True),
    ("last_error_at", sa.DateTime(timezone=True), True),
)


def _existing_columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE_NAME):
        return
    existing = _existing_columns(bind, TABLE_NAME)
    for name, type_, nullable in COLUMNS:
        if name in existing:
            continue
        op.add_column(TABLE_NAME, sa.Column(name, type_, nullable=nullable))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE_NAME):
        return
    existing = _existing_columns(bind, TABLE_NAME)
    for name, _type, _nullable in COLUMNS:
        if name in existing:
            op.drop_column(TABLE_NAME, name)
