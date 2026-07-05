"""Add forwarding_observations table (hop-1 MAC-forwarding harvest).

Ephemeral, periodically-refreshed table holding one row per learned
customer/router MAC and the position it was learned at. The Huawei OLT
harvester (``app/services/network/olt_mac_harvest.py``) parses
``display mac-address port <F/S/P>`` and maps each learned MAC to the exact
PON port (F/S/P) and ONT-ID (the VPI column), the foundation for
ONT<->subscriber drift detection. Read-only toward assignments.

Revision ID: 212_add_forwarding_observations
Revises: 211_seed_unmatched_radio_review_task
Create Date: 2026-07-05
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "212_add_forwarding_observations"
down_revision = "212_crm_invoice_idempotency"
branch_labels = None
depends_on = None

_TABLE = "forwarding_observations"

# ~30-minute beat row scheduling the harvester. Registered as a
# ``scheduled_tasks`` row (the beat builder's generic enabled-rows loop picks it
# up) so the interval stays editable from the admin scheduler UI.
_TASK_NAME = "olt_mac_harvest"
_TASK_PATH = "app.tasks.olt_mac_harvest.run_olt_mac_harvest"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "olt_device_id",
            UUID(as_uuid=True),
            sa.ForeignKey("olt_devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "ont_unit_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ont_units.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "pon_port_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pon_ports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ont_id_on_olt", sa.Integer(), nullable=True),
        sa.Column("mac", sa.Text(), nullable=False),
        sa.Column("vlan", sa.Integer(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default="huawei_olt_mac",
        ),
        sa.UniqueConstraint(
            "olt_device_id",
            "mac",
            "ont_id_on_olt",
            name="uq_forwarding_observations_olt_mac_ont",
        ),
    )
    op.create_index(
        "ix_forwarding_observations_mac",
        _TABLE,
        ["mac"],
    )
    op.create_index(
        "ix_forwarding_observations_observed_at",
        _TABLE,
        ["observed_at"],
    )
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
    if _TABLE not in inspector.get_table_names():
        return
    op.execute(
        sa.text("DELETE FROM scheduled_tasks WHERE name = :name").bindparams(
            name=_TASK_NAME
        )
    )
    op.drop_index("ix_forwarding_observations_observed_at", table_name=_TABLE)
    op.drop_index("ix_forwarding_observations_mac", table_name=_TABLE)
    op.drop_table(_TABLE)
