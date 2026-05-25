"""Merge 109 heads and add IPAM perf indexes.

Adds indexes that make the IP-management page's per-pool aggregation query
table scans become index scans:

  - ipv4_addresses.pool_id
  - ipv6_addresses.pool_id
  - ip_assignments.ipv4_address_id
  - ip_assignments.ipv6_address_id

Revision ID: 110_merge_heads_and_add_ipam_indexes
Revises: 109_convert_support_ticket_status_priority_to_strings, 109_add_customer_identity_index
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op

revision = "110_merge_heads_and_add_ipam_indexes"
down_revision = (
    "109_convert_support_ticket_status_priority_to_strings",
    "109_add_customer_identity_index",
)
branch_labels = None
depends_on = None


_INDEXES = (
    ("ix_ipv4_addresses_pool_id", "ipv4_addresses", "pool_id"),
    ("ix_ipv6_addresses_pool_id", "ipv6_addresses", "pool_id"),
    ("ix_ip_assignments_ipv4_address_id", "ip_assignments", "ipv4_address_id"),
    ("ix_ip_assignments_ipv6_address_id", "ip_assignments", "ipv6_address_id"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/etc — fall back to standard create_index.
        for name, table, col in _INDEXES:
            op.create_index(name, table, [col])
        return
    # On Postgres use IF NOT EXISTS so the migration is idempotent and
    # safe to re-run if a prior failed attempt left some indexes behind.
    for name, table, col in _INDEXES:
        op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{col}")')


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for name, _table, _col in _INDEXES:
            op.drop_index(name)
        return
    for name, _table, _col in _INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
