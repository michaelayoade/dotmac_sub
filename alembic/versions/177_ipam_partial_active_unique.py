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

from alembic import op

revision = "177_ipam_partial_active_unique"
down_revision = "176_availability_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: the squashed-initial migration builds the schema via
    # Base.metadata.create_all() from the *current* model, which already declares
    # the partial-unique indexes — so on a fresh DB the old named constraints never
    # exist and the new indexes already do. Guard with IF EXISTS / IF NOT EXISTS so
    # this both no-ops on a fresh DB and converts an existing (full-unique) prod DB.
    op.execute(
        "ALTER TABLE ip_assignments "
        "DROP CONSTRAINT IF EXISTS uq_ip_assignments_ipv4_address_id"
    )
    op.execute(
        "ALTER TABLE ip_assignments "
        "DROP CONSTRAINT IF EXISTS uq_ip_assignments_ipv6_address_id"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ip_assignments_ipv4_active "
        "ON ip_assignments (ipv4_address_id) WHERE is_active"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ip_assignments_ipv6_active "
        "ON ip_assignments (ipv6_address_id) WHERE is_active"
    )


def downgrade() -> None:
    # Note: re-adding the full-unique fails if multiple inactive rows per address
    # were created while the partial-unique index was in effect.
    op.execute("DROP INDEX IF EXISTS uq_ip_assignments_ipv4_active")
    op.execute("DROP INDEX IF EXISTS uq_ip_assignments_ipv6_active")
    op.execute(
        "ALTER TABLE ip_assignments ADD CONSTRAINT uq_ip_assignments_ipv4_address_id "
        "UNIQUE (ipv4_address_id)"
    )
    op.execute(
        "ALTER TABLE ip_assignments ADD CONSTRAINT uq_ip_assignments_ipv6_address_id "
        "UNIQUE (ipv6_address_id)"
    )
