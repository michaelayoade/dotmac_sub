"""Add reconciler bookkeeping to ont_units and create ont_observations.

Revision ID: 100_add_ont_reconciler_state
Revises: 099_add_device_groups
Create Date: 2026-05-13

Lands the persistence layer for the OLT/ACS reconciler
(``app/services/network/reconcile``). After this migration:

* ``ont_units`` gains five reconciler-bookkeeping columns
  (``sync_status``, ``last_reconciled_at``, ``last_reconcile_started_at``,
  ``last_error``, ``consecutive_sweep_unreachable``).
* A new ``ontsyncstatus`` enum type is created in PostgreSQL.
* A new ``ont_observations`` table holds the 1:1 last-seen OLT + ACS state.

No data backfill — existing rows pick up the defaults (``sync_status='synced'``,
``consecutive_sweep_unreachable=0``). That's correct for the rollout: the
reconciler is inert until later commits wire it up, so claiming "synced" is
truthful until the first sweep runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "100_add_ont_reconciler_state"
down_revision = "099_add_device_groups"
branch_labels = None
depends_on = None


SYNC_STATUS_ENUM_NAME = "ontsyncstatus"
SYNC_STATUS_VALUES = ("synced", "reconciling", "out_of_sync")


def upgrade() -> None:
    # PostgreSQL enum for sync_status. Created explicitly so the downgrade can
    # drop it cleanly; SQLAlchemy's implicit creation can leave stragglers when
    # the column is dropped first.
    sync_status_enum = sa.Enum(
        *SYNC_STATUS_VALUES, name=SYNC_STATUS_ENUM_NAME, create_constraint=False
    )
    sync_status_enum.create(op.get_bind(), checkfirst=True)

    # OntUnit reconciler bookkeeping columns. Defaults are set server-side so
    # existing rows get values without a manual UPDATE.
    op.add_column(
        "ont_units",
        sa.Column(
            "sync_status",
            sa.Enum(
                *SYNC_STATUS_VALUES,
                name=SYNC_STATUS_ENUM_NAME,
                create_type=False,
                create_constraint=False,
            ),
            server_default="synced",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_ont_units_sync_status",
        "ont_units",
        ["sync_status"],
    )
    op.add_column(
        "ont_units",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ont_units_last_reconciled_at",
        "ont_units",
        ["last_reconciled_at"],
    )
    op.add_column(
        "ont_units",
        sa.Column(
            "last_reconcile_started_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column("ont_units", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "ont_units",
        sa.Column(
            "consecutive_sweep_unreachable",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    # OntObservation 1:1 table.
    op.create_table(
        "ont_observations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ont_unit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ont_units.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Reconcile metadata
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reconcile_duration_ms", sa.Integer(), nullable=False),
        sa.Column("mgmt_ip_pingable", sa.Boolean(), nullable=False),
        # OLT-observed
        sa.Column("olt_present", sa.Boolean(), nullable=False),
        sa.Column("olt_match_state", sa.String(length=20), nullable=True),
        sa.Column("olt_run_state", sa.String(length=20), nullable=True),
        sa.Column("olt_distance_m", sa.Integer(), nullable=True),
        sa.Column("olt_rx_dbm", sa.Float(), nullable=True),
        sa.Column("olt_tx_dbm", sa.Float(), nullable=True),
        sa.Column("olt_temperature_c", sa.Integer(), nullable=True),
        sa.Column("olt_description", sa.String(length=128), nullable=True),
        sa.Column("olt_mgmt_ip", sa.String(length=64), nullable=True),
        sa.Column("olt_mgmt_vlan", sa.Integer(), nullable=True),
        sa.Column("olt_line_profile_id", sa.Integer(), nullable=True),
        sa.Column("olt_service_profile_id", sa.Integer(), nullable=True),
        sa.Column("olt_service_ports", sa.JSON(), nullable=True),
        # ACS-observed
        sa.Column("acs_present", sa.Boolean(), nullable=False),
        sa.Column("acs_last_inform_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acs_last_boot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acs_last_bootstrap_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acs_observed_software_version", sa.String(length=120), nullable=True
        ),
        sa.Column("acs_observed_pppoe_username", sa.String(length=120), nullable=True),
        sa.Column("acs_observed_pppoe_enable", sa.Boolean(), nullable=True),
        sa.Column("acs_observed_wan_vlan", sa.Integer(), nullable=True),
        sa.Column(
            "acs_observed_wan_external_ip", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "acs_observed_wan_connection_status", sa.String(length=40), nullable=True
        ),
        sa.Column("acs_observed_nat_enabled", sa.Boolean(), nullable=True),
        sa.Column("acs_observed_dhcp_enabled", sa.Boolean(), nullable=True),
        sa.Column("acs_observed_ssid", sa.String(length=64), nullable=True),
        sa.Column(
            "acs_observed_periodic_inform_interval_sec",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("acs_observed_cr_username_set", sa.Boolean(), nullable=True),
        sa.Column("acs_observed_cr_password_set", sa.Boolean(), nullable=True),
        sa.Column("acs_observed_wan_wcd_index", sa.Integer(), nullable=True),
        sa.Column("acs_observed_wan_instance_index", sa.Integer(), nullable=True),
        # Audit timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("ont_unit_id", name="uq_ont_observations_ont_unit_id"),
    )
    op.create_index(
        "ix_ont_observations_ont_unit_id",
        "ont_observations",
        ["ont_unit_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ont_observations_ont_unit_id", table_name="ont_observations")
    op.drop_table("ont_observations")

    op.drop_column("ont_units", "consecutive_sweep_unreachable")
    op.drop_column("ont_units", "last_error")
    op.drop_column("ont_units", "last_reconcile_started_at")
    op.drop_index("ix_ont_units_last_reconciled_at", table_name="ont_units")
    op.drop_column("ont_units", "last_reconciled_at")
    op.drop_index("ix_ont_units_sync_status", table_name="ont_units")
    op.drop_column("ont_units", "sync_status")

    sa.Enum(name=SYNC_STATUS_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
