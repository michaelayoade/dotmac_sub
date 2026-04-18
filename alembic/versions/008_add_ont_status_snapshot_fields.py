"""add ONT ACS/effective status snapshot fields

Revision ID: 008_add_ont_status_snapshot_fields
Revises: 996c8bca9c16
Create Date: 2026-04-02
"""

import sqlalchemy as sa

from alembic import op

revision = "008_add_ont_status_snapshot_fields"
down_revision = "996c8bca9c16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ont_acs_status = sa.Enum(
        "online",
        "stale",
        "unmanaged",
        "unknown",
        name="ontacsstatus",
        create_constraint=False,
    )
    ont_effective_status = sa.Enum(
        "online",
        "offline",
        "unknown",
        name="onteffectivestatus",
        create_constraint=False,
    )
    ont_status_source = sa.Enum(
        "olt",
        "acs",
        "derived",
        name="ontstatussource",
        create_constraint=False,
    )
    ont_acs_status.create(bind, checkfirst=True)
    ont_effective_status.create(bind, checkfirst=True)
    ont_status_source.create(bind, checkfirst=True)

    op.add_column(
        "ont_units",
        sa.Column(
            "acs_status",
            ont_acs_status,
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column("acs_last_inform_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column(
            "effective_status",
            ont_effective_status,
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column(
            "effective_status_source",
            ont_status_source,
            nullable=False,
            server_default="derived",
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column("status_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        UPDATE ont_units
        SET effective_status = COALESCE(CAST(online_status AS TEXT), 'unknown')::onteffectivestatus,
            effective_status_source = 'olt'::ontstatussource,
            status_resolved_at = CURRENT_TIMESTAMP
        """
    )


def downgrade() -> None:
    op.drop_column("ont_units", "status_resolved_at")
    op.drop_column("ont_units", "effective_status_source")
    op.drop_column("ont_units", "effective_status")
    op.drop_column("ont_units", "acs_last_inform_at")
    op.drop_column("ont_units", "acs_status")
