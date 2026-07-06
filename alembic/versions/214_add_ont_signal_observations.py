"""Add ont_signal_observations table (per-ONT status + Rx time series).

Append-only snapshot of every ONT's ``olt_status`` and ``onu_rx_signal_dbm``,
one row per collection sweep. It is the time-series substrate the live
``ont_units`` columns lack, feeding splice inference (design §6): co-failure
clustering and correlated-Rx droop detection recover the unpollable sub-PON
splitter branches (design §4). Also seeds a ~30-minute beat row for the
collector task ``app.tasks.ont_signal_observations.record_ont_observations``.

Revision ID: 214_add_ont_signal_observations
Revises: 213_add_payment_refunded_amount
Create Date: 2026-07-06
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "214_add_ont_signal_observations"
down_revision = "213_add_payment_refunded_amount"
branch_labels = None
depends_on = None

_TABLE = "ont_signal_observations"
_TASK_NAME = "ont_signal_observations"
_TASK_PATH = "app.tasks.ont_signal_observations.record_ont_observations"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "ont_unit_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ont_units.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "olt_device_id",
                UUID(as_uuid=True),
                sa.ForeignKey("olt_devices.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "pon_port_id",
                UUID(as_uuid=True),
                sa.ForeignKey("pon_ports.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Reuse the existing onuonlinestatus enum (create_type=False so this
            # migration never tries to re-CREATE TYPE — ont_units already owns it).
            sa.Column(
                "olt_status",
                sa.Enum(
                    "online",
                    "offline",
                    name="onuonlinestatus",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column("rx_signal_dbm", sa.Float(), nullable=True),
            sa.Column(
                "observed_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "ix_ont_signal_observations_ont_observed",
            _TABLE,
            ["ont_unit_id", "observed_at"],
        )
        op.create_index(
            "ix_ont_signal_observations_pon_observed",
            _TABLE,
            ["pon_port_id", "observed_at"],
        )

    # Seed the collector beat row (generic enabled-rows loop in the beat builder
    # picks it up; interval stays editable from the admin scheduler UI).
    op.execute(
        sa.text(
            """
            INSERT INTO scheduled_tasks
                (id, name, task_name, schedule_type, interval_seconds,
                 enabled, created_at, updated_at)
            SELECT :id, :name, :task_path, 'interval', 1800,
                   true, now(), now()
            WHERE NOT EXISTS (
                SELECT 1 FROM scheduled_tasks WHERE name = :name
            )
            """
        ).bindparams(
            sa.bindparam("id", value=uuid4(), type_=sa.Uuid()),
            sa.bindparam("name", value=_TASK_NAME),
            sa.bindparam("task_path", value=_TASK_PATH),
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute(
        sa.text("DELETE FROM scheduled_tasks WHERE name = :name").bindparams(
            name=_TASK_NAME
        )
    )
    if _TABLE in inspector.get_table_names():
        op.drop_index("ix_ont_signal_observations_pon_observed", table_name=_TABLE)
        op.drop_index("ix_ont_signal_observations_ont_observed", table_name=_TABLE)
        op.drop_table(_TABLE)
