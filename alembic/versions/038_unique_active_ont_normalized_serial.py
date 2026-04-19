"""enforce active ONT normalized serial uniqueness

Revision ID: 039_unique_active_ont_normalized_serial
Revises: 038_add_ont_provisioning_events
Create Date: 2026-04-19
"""

from alembic import op

revision = "039_unique_active_ont_normalized_serial"
down_revision = "038_add_ont_provisioning_events"
branch_labels = None
depends_on = None


_NORMALIZED_SERIAL_EXPR = (
    "regexp_replace(upper(coalesce(serial_number, '')), '[^A-Z0-9]', '', 'g')"
)
_INDEX_NAME = "uq_ont_units_active_normalized_serial"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME}
        ON ont_units (({_NORMALIZED_SERIAL_EXPR}))
        WHERE is_active IS TRUE
          AND {_NORMALIZED_SERIAL_EXPR} <> ''
          AND serial_number !~* '^(HW|ZT|NK|OLT)-'
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
