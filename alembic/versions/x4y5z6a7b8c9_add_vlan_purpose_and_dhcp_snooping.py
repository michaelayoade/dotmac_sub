"""Add purpose and dhcp_snooping columns to vlans table.

Revision ID: x4y5z6a7b8c9
Revises: d5e6f7a8b9c0, w3x4y5z6a7b8
Create Date: 2026-02-25
"""

import sqlalchemy as sa

from alembic import op

revision = "x4y5z6a7b8c9"
down_revision = ("d5e6f7a8b9c0", "w3x4y5z6a7b8")
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Create the vlanpurpose enum type if it doesn't exist
    # This must happen outside of a transaction block for PostgreSQL
    op.execute(
        "DO $$ BEGIN "
        "  CREATE TYPE vlanpurpose AS ENUM "
        "    ('internet', 'management', 'tr069', 'iptv', 'voip', 'other'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )

    existing_columns = [c["name"] for c in inspector.get_columns("vlans")]

    if "purpose" not in existing_columns:
        op.add_column(
            "vlans",
            sa.Column(
                "purpose",
                sa.Enum(
                    "internet",
                    "management",
                    "tr069",
                    "iptv",
                    "voip",
                    "other",
                    name="vlanpurpose",
                    create_constraint=False,
                ),
                nullable=True,
            ),
        )

    if "dhcp_snooping" not in existing_columns:
        op.add_column(
            "vlans",
            sa.Column(
                "dhcp_snooping",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    existing_columns = [c["name"] for c in inspector.get_columns("vlans")]

    if "dhcp_snooping" in existing_columns:
        op.drop_column("vlans", "dhcp_snooping")

    if "purpose" in existing_columns:
        op.drop_column("vlans", "purpose")

    # Drop the enum type
    op.execute("DROP TYPE IF EXISTS vlanpurpose")
