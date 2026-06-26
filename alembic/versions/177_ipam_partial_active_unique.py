"""IPAM: partial-unique active assignment (re-allocate released IPs)

Replace the full UNIQUE(ipv4_address_id) / UNIQUE(ipv6_address_id) on
ip_assignments with PARTIAL unique indexes that apply only WHERE is_active. This
lets a terminally-released assignment stay (is_active=false) for history while the
address becomes re-allocatable to a new subscriber — fixing the "asymmetric
release" pool-exhaustion bug where a released IP showed free in the UI but the
allocator (which skipped any address carrying an assignment row) could never hand
it out again.

Safe forward: the current full-unique guarantees at most one row per address, so
at most one active row exists today — the partial-unique invariant already holds.

Revision ID: 177_ipam_partial_active_unique
Revises: 176_availability_snapshots
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "177_ipam_partial_active_unique"
down_revision = "176_availability_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_ip_assignments_ipv4_address_id", "ip_assignments", type_="unique"
    )
    op.drop_constraint(
        "uq_ip_assignments_ipv6_address_id", "ip_assignments", type_="unique"
    )
    op.create_index(
        "uq_ip_assignments_ipv4_active",
        "ip_assignments",
        ["ipv4_address_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "uq_ip_assignments_ipv6_active",
        "ip_assignments",
        ["ipv6_address_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    # Note: fails if multiple inactive rows per address were created while the
    # partial-unique index was in effect (the full-unique can no longer hold).
    op.drop_index("uq_ip_assignments_ipv4_active", table_name="ip_assignments")
    op.drop_index("uq_ip_assignments_ipv6_active", table_name="ip_assignments")
    op.create_unique_constraint(
        "uq_ip_assignments_ipv4_address_id", "ip_assignments", ["ipv4_address_id"]
    )
    op.create_unique_constraint(
        "uq_ip_assignments_ipv6_address_id", "ip_assignments", ["ipv6_address_id"]
    )
