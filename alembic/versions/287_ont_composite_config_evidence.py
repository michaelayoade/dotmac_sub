"""Add composite desired and observed evidence to ONT snapshots.

Revision ID: 287_ont_composite_config_evidence
Revises: 286_huawei_tr069_profile_sot
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "287_ont_composite_config_evidence"
down_revision = "286_huawei_tr069_profile_sot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_config_snapshots",
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "ont_config_snapshots", sa.Column("effective_config", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ont_config_snapshots", sa.Column("observed_state", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ont_config_snapshots", sa.Column("provenance", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ont_config_snapshots",
        sa.Column("payload_checksum", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_config_snapshots", "payload_checksum")
    op.drop_column("ont_config_snapshots", "provenance")
    op.drop_column("ont_config_snapshots", "observed_state")
    op.drop_column("ont_config_snapshots", "effective_config")
    op.drop_column("ont_config_snapshots", "schema_version")
