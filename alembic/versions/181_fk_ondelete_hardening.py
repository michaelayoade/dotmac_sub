"""Harden FK ondelete: preserve revenue + stop inventory cascade-wipe

From the ondelete FK delete-safety review (docs/audits/ONDELETE_FK_REVIEW.md):
- mrr_snapshots.subscriber_id: CASCADE -> SET NULL (+ nullable) so a subscriber
  hard-delete preserves revenue history instead of wiping it.
- 5 dormant network-inventory FKs: CASCADE -> RESTRICT so a future hard-delete of
  an OLT/ONT/port-pool can't silently wipe active inventory/allocations.

Postgres-only (raw FK rebuild); fresh/SQLite schemas come from the current model
via create_all(), so the no-op guard keeps this idempotent. Each FK is looked up
by column (name-agnostic) then dropped + recreated with the new ON DELETE.

Revision ID: 181_fk_ondelete_hardening
Revises: 180_import_runs
"""

from __future__ import annotations

from alembic import op

revision = "181_fk_ondelete_hardening"
down_revision = "180_import_runs"
branch_labels = None
depends_on = None

# (table, column, parent_table, ondelete)
_FK_SPECS = [
    ("mrr_snapshots", "subscriber_id", "subscribers", "SET NULL"),
    ("olt_ont_registrations", "olt_id", "olt_devices", "RESTRICT"),
    ("ont_wan_service_instances", "ont_id", "ont_units", "RESTRICT"),
    ("service_port_allocations", "pool_id", "olt_service_port_pools", "RESTRICT"),
    ("service_port_allocations", "ont_unit_id", "ont_units", "RESTRICT"),
    ("olt_service_ports", "olt_device_id", "olt_devices", "RESTRICT"),
]


def _rebuild_fk(table: str, column: str, parent: str, ondelete: str) -> str:
    """Drop the (single-column) FK on table.column, recreate with the ondelete."""
    return f"""
DO $$
DECLARE cname text;
BEGIN
  IF to_regclass('{table}') IS NULL OR to_regclass('{parent}') IS NULL THEN
    RETURN;
  END IF;
  SELECT con.conname INTO cname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_attribute att ON att.attrelid = con.conrelid
                         AND att.attnum = ANY(con.conkey)
    WHERE con.contype = 'f' AND rel.relname = '{table}'
      AND att.attname = '{column}' AND array_length(con.conkey, 1) = 1
    LIMIT 1;
  IF cname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', cname);
  END IF;
  ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_fkey
    FOREIGN KEY ({column}) REFERENCES {parent}(id) ON DELETE {ondelete};
END $$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE mrr_snapshots ALTER COLUMN subscriber_id DROP NOT NULL")
    for spec in _FK_SPECS:
        op.execute(_rebuild_fk(*spec))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Best-effort reversal (restores the prior, riskier behavior).
    for table, column, parent in [
        ("olt_ont_registrations", "olt_id", "olt_devices"),
        ("ont_wan_service_instances", "ont_id", "ont_units"),
        ("service_port_allocations", "pool_id", "olt_service_port_pools"),
        ("service_port_allocations", "ont_unit_id", "ont_units"),
        ("olt_service_ports", "olt_device_id", "olt_devices"),
        ("mrr_snapshots", "subscriber_id", "subscribers"),
    ]:
        op.execute(_rebuild_fk(table, column, parent, "CASCADE"))
