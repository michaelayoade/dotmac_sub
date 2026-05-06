"""Clear OLT chassis identifiers from ONT model values.

Revision ID: 093_clear_ont_chassis_model_values
Revises: 092_remove_legacy_olt_config_pack_profile_defaults
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "093_clear_ont_chassis_model_values"
down_revision = "092_remove_legacy_olt_config_pack_profile_defaults"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _column_exists("ont_units", "model"):
        return

    op.execute(
        sa.text(
            """
            UPDATE ont_units
            SET model = NULL
            WHERE model ~* '^(HUAWEI[[:space:]]+)?(MA56(00|08T|83T)|MA58(00|08)(-[A-Z0-9]+)?|MA5600V[A-Z0-9]*|MA5800V[A-Z0-9]*)$'
            """
        )
    )


def downgrade() -> None:
    # Irreversible cleanup: these values are OLT chassis identifiers, not ONT
    # equipment IDs. Restoring them would reintroduce invalid profile mappings.
    pass
