"""Make field position evidence replay-safe and provider-neutral.

Revision ID: 542_position_observation_identity
Revises: 541_staff_session_party_ratchet
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "542_position_observation_identity"
down_revision = "541_staff_session_party_ratchet"
branch_labels = None
depends_on = None

_PRESENCE_TABLE = "field_tech_presence"
_PING_TABLE = "field_tech_location_pings"


def _column_names(table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _column_nullable(table_name: str, column_name: str) -> bool:
    for column in sa.inspect(op.get_bind()).get_columns(table_name):
        if column["name"] == column_name:
            return bool(column["nullable"])
    raise RuntimeError(f"missing expected column {table_name}.{column_name}")


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _check_names(table_name: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    if "ck_field_tech_presence_status" in _check_names(_PRESENCE_TABLE):
        op.drop_constraint(
            "ck_field_tech_presence_status",
            _PRESENCE_TABLE,
            type_="check",
        )
    op.execute(
        sa.text(
            "UPDATE field_tech_presence SET status = 'on_break' WHERE status = 'break'"
        )
    )
    op.create_check_constraint(
        "ck_field_tech_presence_status",
        "field_tech_presence",
        "status IN ('off_shift', 'on_shift', 'on_break', 'busy')",
    )
    presence_columns = _column_names(_PRESENCE_TABLE)
    if "collection_purpose" not in presence_columns:
        op.add_column(
            _PRESENCE_TABLE,
            sa.Column("collection_purpose", sa.String(length=32), nullable=True),
        )
    if "collection_granted_at" not in presence_columns:
        op.add_column(
            _PRESENCE_TABLE,
            sa.Column(
                "collection_granted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if "collection_expires_at" not in presence_columns:
        op.add_column(
            _PRESENCE_TABLE,
            sa.Column(
                "collection_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    # A legacy global sharing boolean has no attributable purpose or expiry.
    # Disable it instead of fabricating consent; the technician explicitly
    # starts a bounded collection lease after deployment.
    op.execute(
        sa.text(
            """
            UPDATE field_tech_presence
            SET location_sharing_enabled = false,
                status = 'off_shift'
            WHERE location_sharing_enabled IS TRUE
            """
        )
    )
    if "ck_field_tech_presence_active_collection_grant" not in _check_names(
        _PRESENCE_TABLE
    ):
        op.create_check_constraint(
            "ck_field_tech_presence_active_collection_grant",
            _PRESENCE_TABLE,
            "NOT location_sharing_enabled OR ("
            "collection_purpose IS NOT NULL AND "
            "collection_granted_at IS NOT NULL AND "
            "collection_expires_at IS NOT NULL AND "
            "collection_expires_at > collection_granted_at)",
        )

    ping_columns = _column_names(_PING_TABLE)
    if "crm_work_order_id" in ping_columns and "work_order_id" in ping_columns:
        raise RuntimeError(
            "field position migration found both legacy and canonical work-order columns"
        )
    if "crm_work_order_id" in ping_columns:
        if "ix_field_tech_location_pings_crm_work_order_id" in _index_names(
            _PING_TABLE
        ):
            op.drop_index(
                "ix_field_tech_location_pings_crm_work_order_id",
                table_name=_PING_TABLE,
            )
        op.alter_column(
            _PING_TABLE,
            "crm_work_order_id",
            new_column_name="work_order_id",
            existing_type=sa.String(length=64),
            existing_nullable=True,
        )
    elif "work_order_id" not in ping_columns:
        raise RuntimeError(
            "field position migration found no work-order identity column"
        )
    if "ix_field_tech_location_pings_work_order_id" not in _index_names(_PING_TABLE):
        op.create_index(
            "ix_field_tech_location_pings_work_order_id",
            _PING_TABLE,
            ["work_order_id"],
        )

    ping_columns = _column_names(_PING_TABLE)
    if "client_observation_id" not in ping_columns:
        op.add_column(
            _PING_TABLE,
            sa.Column(
                "client_observation_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    if "payload_fingerprint" not in ping_columns:
        op.add_column(
            _PING_TABLE,
            sa.Column("payload_fingerprint", sa.String(length=64), nullable=True),
        )
    op.execute(
        sa.text(
            """
            UPDATE field_tech_location_pings
            SET client_observation_id = id,
                payload_fingerprint = 'legacy:' || id::text
            WHERE client_observation_id IS NULL
               OR payload_fingerprint IS NULL
            """
        )
    )
    if _column_nullable(_PING_TABLE, "client_observation_id"):
        op.alter_column(
            _PING_TABLE,
            "client_observation_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )
    if _column_nullable(_PING_TABLE, "payload_fingerprint"):
        op.alter_column(
            _PING_TABLE,
            "payload_fingerprint",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    if "ux_field_tech_location_pings_observation_identity" not in _index_names(
        _PING_TABLE
    ):
        op.create_index(
            "ux_field_tech_location_pings_observation_identity",
            _PING_TABLE,
            ["technician_id", "source", "client_observation_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    op.drop_constraint(
        "ck_field_tech_presence_active_collection_grant",
        "field_tech_presence",
        type_="check",
    )
    op.drop_column("field_tech_presence", "collection_expires_at")
    op.drop_column("field_tech_presence", "collection_granted_at")
    op.drop_column("field_tech_presence", "collection_purpose")

    op.drop_index(
        "ux_field_tech_location_pings_observation_identity",
        table_name="field_tech_location_pings",
    )
    op.drop_column("field_tech_location_pings", "payload_fingerprint")
    op.drop_column("field_tech_location_pings", "client_observation_id")

    op.drop_index(
        "ix_field_tech_location_pings_work_order_id",
        table_name="field_tech_location_pings",
    )
    op.alter_column(
        "field_tech_location_pings",
        "work_order_id",
        new_column_name="crm_work_order_id",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.create_index(
        "ix_field_tech_location_pings_crm_work_order_id",
        "field_tech_location_pings",
        ["crm_work_order_id"],
    )

    op.drop_constraint(
        "ck_field_tech_presence_status",
        "field_tech_presence",
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE field_tech_presence SET status = 'break' WHERE status = 'on_break'"
        )
    )
    op.create_check_constraint(
        "ck_field_tech_presence_status",
        "field_tech_presence",
        "status IN ('off_shift', 'on_shift', 'break', 'busy')",
    )
