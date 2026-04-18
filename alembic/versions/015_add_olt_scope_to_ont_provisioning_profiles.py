"""add olt scope to ont provisioning profiles

Revision ID: 015_add_olt_scope_to_ont_provisioning_profiles
Revises: 014_optimize_tr069_inform_storage
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "015_add_olt_scope_to_ont_provisioning_profiles"
down_revision = "014_optimize_tr069_inform_storage"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def _fk_names(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {fk["name"] for fk in inspector.get_foreign_keys(table_name)}


def _index_names(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _has_column("ont_provisioning_profiles", "olt_device_id"):
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("olt_device_id", sa.UUID(), nullable=True),
        )

    if "fk_ont_provisioning_profiles_olt_device_id" not in _fk_names(
        "ont_provisioning_profiles"
    ):
        op.create_foreign_key(
            "fk_ont_provisioning_profiles_olt_device_id",
            "ont_provisioning_profiles",
            "olt_devices",
            ["olt_device_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "ix_ont_provisioning_profiles_olt_device_id" not in _index_names(
        "ont_provisioning_profiles"
    ):
        op.create_index(
            "ix_ont_provisioning_profiles_olt_device_id",
            "ont_provisioning_profiles",
            ["olt_device_id"],
        )


def downgrade() -> None:
    if "ix_ont_provisioning_profiles_olt_device_id" in _index_names(
        "ont_provisioning_profiles"
    ):
        op.drop_index(
            "ix_ont_provisioning_profiles_olt_device_id",
            table_name="ont_provisioning_profiles",
        )
    if "fk_ont_provisioning_profiles_olt_device_id" in _fk_names(
        "ont_provisioning_profiles"
    ):
        op.drop_constraint(
            "fk_ont_provisioning_profiles_olt_device_id",
            "ont_provisioning_profiles",
            type_="foreignkey",
        )
    if _has_column("ont_provisioning_profiles", "olt_device_id"):
        op.drop_column("ont_provisioning_profiles", "olt_device_id")
