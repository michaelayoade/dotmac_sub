"""Move ONT access flags into desired_config.

Revision ID: 086_move_ont_access_flags
Revises: 085_drop_ont_unit_legacy_lan_wifi
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "086_move_ont_access_flags"
down_revision = "085_drop_ont_unit_legacy_lan_wifi"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _drop_column_if_exists(inspector: sa.Inspector, table: str, column: str) -> None:
    if _column_exists(inspector, table, column):
        op.drop_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    has_wan_remote = _column_exists(inspector, "ont_units", "wan_remote_access")
    has_mgmt_remote = _column_exists(inspector, "ont_units", "mgmt_remote_access")

    if has_wan_remote or has_mgmt_remote:
        access_fields = []
        where_clauses = []
        if has_wan_remote:
            access_fields.append("'wan_remote', wan_remote_access")
            where_clauses.append("wan_remote_access IS NOT NULL")
        if has_mgmt_remote:
            access_fields.append("'mgmt_remote', mgmt_remote_access")
            where_clauses.append("mgmt_remote_access IS NOT NULL")

        bind.execute(
            sa.text(
                f"""
                UPDATE ont_units
                SET desired_config = COALESCE(desired_config, '{{}}'::jsonb)
                    || jsonb_build_object(
                        'access',
                        jsonb_strip_nulls(
                            jsonb_build_object(
                                {", ".join(access_fields)}
                            )
                        )
                        || COALESCE(desired_config->'access', '{{}}'::jsonb)
                    )
                WHERE {" OR ".join(where_clauses)}
                """
            )
        )

    _drop_column_if_exists(inspector, "ont_units", "wan_remote_access")
    _drop_column_if_exists(inspector, "ont_units", "mgmt_remote_access")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "ont_units", "wan_remote_access"):
        op.add_column(
            "ont_units",
            sa.Column(
                "wan_remote_access",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )
    if not _column_exists(inspector, "ont_units", "mgmt_remote_access"):
        op.add_column(
            "ont_units",
            sa.Column(
                "mgmt_remote_access",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )

    bind.execute(
        sa.text(
            """
            UPDATE ont_units
            SET
                wan_remote_access = COALESCE(
                    (desired_config #>> '{access,wan_remote}')::boolean,
                    false
                ),
                mgmt_remote_access = COALESCE(
                    (desired_config #>> '{access,mgmt_remote}')::boolean,
                    false
                )
            WHERE desired_config ? 'access'
            """
        )
    )
