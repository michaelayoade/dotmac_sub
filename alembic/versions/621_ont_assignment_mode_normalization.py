"""Normalize and constrain legacy ONT assignment mode columns.

Revision ID: 621_ont_assignment_mode_normalization
Revises: 620_zeptomail_delivery_statuses
Create Date: 2026-09-25
"""

from __future__ import annotations

from alembic import op

revision = "621_ont_assignment_mode_normalization"
down_revision = "620_zeptomail_delivery_statuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These columns are legacy compatibility fields. Normalize known aliases;
    # unknown historical values become NULL because this retired projection is
    # not authoritative and guessing a service mode would invent intent.
    op.execute(
        """
        UPDATE ont_assignments
        SET wan_mode = CASE lower(btrim(wan_mode))
            WHEN 'bridge' THEN 'bridging'
            WHEN 'bridged' THEN 'bridging'
            WHEN 'setup_via_onu' THEN 'bridging'
            WHEN 'routing' THEN 'routing'
            WHEN 'bridging' THEN 'bridging'
            ELSE NULL
        END
        WHERE wan_mode IS NOT NULL
          AND lower(btrim(wan_mode)) NOT IN ('routing', 'bridging')
        """
    )
    op.execute(
        """
        UPDATE ont_assignments
        SET ip_mode = CASE lower(btrim(ip_mode))
            WHEN 'bridge' THEN 'dhcp'
            WHEN 'bridged' THEN 'dhcp'
            WHEN 'dynamic' THEN 'dhcp'
            WHEN 'static' THEN 'static_ip'
            WHEN 'inactive' THEN 'inactive'
            WHEN 'static_ip' THEN 'static_ip'
            WHEN 'dhcp' THEN 'dhcp'
            ELSE NULL
        END
        WHERE ip_mode IS NOT NULL
          AND lower(btrim(ip_mode)) NOT IN ('inactive', 'static_ip', 'dhcp')
        """
    )
    op.create_check_constraint(
        "ck_ont_assignments_wan_mode_valid",
        "ont_assignments",
        "wan_mode IS NULL OR wan_mode IN ('routing', 'bridging')",
    )
    op.create_check_constraint(
        "ck_ont_assignments_ip_mode_valid",
        "ont_assignments",
        "ip_mode IS NULL OR ip_mode IN ('inactive', 'static_ip', 'dhcp')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ont_assignments_ip_mode_valid", "ont_assignments", type_="check"
    )
    op.drop_constraint(
        "ck_ont_assignments_wan_mode_valid", "ont_assignments", type_="check"
    )
    # Normalized values are intentionally not changed back to invalid aliases.
