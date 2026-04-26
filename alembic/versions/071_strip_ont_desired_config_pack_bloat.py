"""Strip OLT config-pack fields from ONT desired_config.

Revision ID: 071_strip_ont_desired_config_pack_bloat
Revises: 070_single_active_ont_assignment
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op

revision = "071_strip_ont_desired_config_pack_bloat"
down_revision = "070_single_active_ont_assignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        WITH cleaned AS (
            SELECT
                id,
                (
                    (
                        (
                            (
                                (
                                    (
                                        COALESCE(desired_config, '{}'::jsonb)
                                        #- '{tr069}'
                                    )
                                    #- '{authorization}'
                                )
                                #- '{omci}'
                            )
                            #- '{wan,vlan}'
                        )
                        #- '{wan,gem_index}'
                    )
                    #- '{management,vlan}'
                ) AS config
            FROM ont_units
            WHERE desired_config IS NOT NULL
        ),
        pruned_wan AS (
            SELECT
                id,
                CASE
                    WHEN config->'wan' = '{}'::jsonb THEN config - 'wan'
                    ELSE config
                END AS config
            FROM cleaned
        ),
        pruned_management AS (
            SELECT
                id,
                CASE
                    WHEN config->'management' = '{}'::jsonb THEN config - 'management'
                    ELSE config
                END AS config
            FROM pruned_wan
        )
        UPDATE ont_units AS ou
        SET desired_config = pruned_management.config
        FROM pruned_management
        WHERE ou.id = pruned_management.id
          AND ou.desired_config IS DISTINCT FROM pruned_management.config
        """
    )


def downgrade() -> None:
    # Removed JSON keys cannot be reconstructed safely.
    pass
