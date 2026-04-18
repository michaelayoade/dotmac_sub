"""Add provisioning profile OLT knobs and ONT TR-069 data model field.

Adds to ont_provisioning_profiles:
  - internet_config_ip_index, wan_config_profile_id, pppoe_omci_vlan
  - cr_username, cr_password

Adds to ont_units:
  - tr069_data_model

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-03-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# (table, column_name, column_type, kwargs)
_PROFILE_COLUMNS: list[tuple[str, str, sa.types.TypeEngine, dict]] = [
    (
        "ont_provisioning_profiles",
        "internet_config_ip_index",
        sa.Integer(),
        {"server_default": "0"},
    ),
    (
        "ont_provisioning_profiles",
        "wan_config_profile_id",
        sa.Integer(),
        {"server_default": "0"},
    ),
    ("ont_provisioning_profiles", "pppoe_omci_vlan", sa.Integer(), {}),
    ("ont_provisioning_profiles", "cr_username", sa.String(120), {}),
    ("ont_provisioning_profiles", "cr_password", sa.String(120), {}),
]

_ONT_COLUMNS: list[tuple[str, str, sa.types.TypeEngine, dict]] = [
    ("ont_units", "tr069_data_model", sa.String(40), {}),
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    for table, col_name, col_type, kwargs in _PROFILE_COLUMNS + _ONT_COLUMNS:
        columns = [c["name"] for c in inspector.get_columns(table)]
        if col_name not in columns:
            op.add_column(
                table,
                sa.Column(col_name, col_type, nullable=True, **kwargs),
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    for table, col_name, _col_type, _kwargs in reversed(
        _PROFILE_COLUMNS + _ONT_COLUMNS
    ):
        columns = [c["name"] for c in inspector.get_columns(table)]
        if col_name in columns:
            op.drop_column(table, col_name)
