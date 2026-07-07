"""Add saved network weathermap views.

Revision ID: 220_add_network_weathermap_views
Revises: 219_add_drift_finding_evidence
Create Date: 2026-07-07
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "220_add_network_weathermap_views"
down_revision = "219_add_drift_finding_evidence"
branch_labels = None
depends_on = None

_TABLE = "network_weathermap_views"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE in inspector.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("topology_group", sa.String(length=80), nullable=True),
        sa.Column("pop_site_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("layout", sa.JSON(), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pop_site_id"], ["pop_sites.id"]),
        sa.UniqueConstraint("slug", name="uq_network_weathermap_views_slug"),
    )
    op.create_index("ix_network_weathermap_views_pop_site", _TABLE, ["pop_site_id"])
    op.create_index("ix_network_weathermap_views_default", _TABLE, ["is_default"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    op.drop_index("ix_network_weathermap_views_default", table_name=_TABLE)
    op.drop_index("ix_network_weathermap_views_pop_site", table_name=_TABLE)
    op.drop_table(_TABLE)
