"""Add blocked/hidden status values and fix Splynx status mappings.

Adds 'blocked' to subscriberstatus and subscriptionstatus enums,
adds 'hidden' to subscriptionstatus enum, to match Splynx semantics exactly.

Then fixes migrated data:
- Splynx 'blocked' customers: suspended → blocked (2,850 rows)
- Splynx 'disabled' customers wrongly set to canceled: → disabled (89 rows)
- Splynx 'blocked' services: suspended → blocked
- Splynx 'hidden' services: archived → hidden

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-03-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if table_name not in tables:
        return False
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _get_enum_names(bind) -> set[str]:
    rows = bind.execute(sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
    return {row[0] for row in rows}


def _add_enum_value_if_missing(bind, enum_name: str, value: str) -> None:
    escaped_value = value.replace("'", "''")
    bind.execute(
        sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{escaped_value}'")
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing_enums = _get_enum_names(bind)

    # --- 1. Add 'blocked' to subscriberstatus enum ---
    if "subscriberstatus" in existing_enums:
        _add_enum_value_if_missing(bind, "subscriberstatus", "blocked")

    # --- 2. Add 'blocked' and 'hidden' to subscriptionstatus enum ---
    if "subscriptionstatus" in existing_enums:
        _add_enum_value_if_missing(bind, "subscriptionstatus", "blocked")
        _add_enum_value_if_missing(bind, "subscriptionstatus", "hidden")

    # Commit the enum changes so they can be used in UPDATE statements
    op.execute("COMMIT")

    # Skip data fixes on fresh DBs where splynx/metadata columns don't exist
    if not _column_exists(bind, "subscribers", "splynx_customer_id"):
        return
    if not _column_exists(bind, "subscribers", "metadata"):
        return

    # --- 3. Fix subscriber statuses based on Splynx metadata ---

    # Splynx 'blocked' customers: suspended → blocked
    op.execute("""
        UPDATE subscribers
        SET status = 'blocked'::subscriberstatus
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_status' = 'blocked'
          AND status = 'suspended'
    """)

    # Splynx 'disabled' customers wrongly mapped to canceled: → disabled
    op.execute("""
        UPDATE subscribers
        SET status = 'disabled'::subscriberstatus
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_status' = 'disabled'
          AND metadata->>'splynx_deleted' IS NULL
          AND status = 'canceled'
    """)

    # Splynx 'new' customers mapped to active: → new
    op.execute("""
        UPDATE subscribers
        SET status = 'new'::subscriberstatus
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_status' = 'new'
          AND metadata->>'splynx_deleted' IS NULL
          AND status = 'active'
    """)

    # --- 4. Fix subscription statuses ---
    # The SQL migration mapped Splynx 'blocked' → 'suspended' and 'hidden' → 'archived'.
    # We can't reliably distinguish these from DotMac-native suspended/archived,
    # so we fix only subscriptions belonging to Splynx-imported subscribers
    # whose customer was 'blocked' in Splynx.

    # Subscriptions from blocked customers: suspended → blocked
    op.execute("""
        UPDATE subscriptions
        SET status = 'blocked'::subscriptionstatus
        WHERE status = 'suspended'
          AND subscriber_id IN (
              SELECT id FROM subscribers
              WHERE splynx_customer_id IS NOT NULL
                AND metadata->>'splynx_status' = 'blocked'
          )
    """)


def downgrade() -> None:
    bind = op.get_bind()

    # Revert subscription status: blocked → suspended
    op.execute("""
        UPDATE subscriptions
        SET status = 'suspended'::subscriptionstatus
        WHERE status = 'blocked'
    """)

    # Revert subscriber statuses: blocked → suspended, new overrides → active
    op.execute("""
        UPDATE subscribers
        SET status = 'suspended'::subscriberstatus
        WHERE status = 'blocked'
    """)

    if not _column_exists(bind, "subscribers", "splynx_customer_id"):
        return

    op.execute("""
        UPDATE subscribers
        SET status = 'active'::subscriberstatus
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_status' = 'new'
          AND status = 'new'
    """)

    op.execute("""
        UPDATE subscribers
        SET status = 'canceled'::subscriberstatus
        WHERE splynx_customer_id IS NOT NULL
          AND metadata->>'splynx_status' = 'disabled'
          AND metadata->>'splynx_deleted' IS NULL
          AND status = 'disabled'
    """)

    # Note: PostgreSQL does not support removing enum values.
    # The 'blocked' and 'hidden' values remain in the enum types.
