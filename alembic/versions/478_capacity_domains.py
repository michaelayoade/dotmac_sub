"""Capacity domains — shared segments and what they can carry.

Contention was an integer on a catalogue offer that nothing enforced and
nothing measured. Nothing in the access layer records bandwidth capacity at
all: ``pon_ports.max_ont_capacity`` is a device count and is NULL on every row,
``wireless_masts`` has location and height only, and no OLT uplink or BNG
figure exists anywhere. So neither a contention ratio nor a committed rate
could ever be checked against reality.

PON ports, wireless sectors, OLT uplinks and BNGs are different hardware but
the same idea — a bandwidth ceiling with subscribers behind it — so they share
one table rather than growing three capacity checks that disagree.

``downstream_mbps``/``upstream_mbps`` are NULLABLE on purpose: a segment must
be nameable before it is surveyed, or the survey backlog cannot be enumerated.
NULL reads as unknown and never as healthy. Zero or negative is still refused —
a bad measurement and a missing one must not look alike.

No rows are seeded. Capacity is recorded, never inferred: split ratio, XGS-PON
upgrades and shared uplinks all move the real number, and a guessed figure
produces a check that quietly passes.

Revision ID: 478_capacity_domains
Revises: 477_service_handoffs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "478_capacity_domains"
down_revision: str | None = "477_service_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_MATCHES_KIND = (
    "(kind = 'pon_port' AND pon_port_id IS NOT NULL "
    " AND wireless_mast_id IS NULL AND olt_id IS NULL AND nas_device_id IS NULL) "
    "OR (kind = 'wireless_sector' AND wireless_mast_id IS NOT NULL "
    " AND pon_port_id IS NULL AND olt_id IS NULL AND nas_device_id IS NULL) "
    "OR (kind = 'olt_uplink' AND olt_id IS NOT NULL "
    " AND pon_port_id IS NULL AND wireless_mast_id IS NULL "
    " AND nas_device_id IS NULL) "
    "OR (kind = 'bng' AND nas_device_id IS NOT NULL "
    " AND pon_port_id IS NULL AND wireless_mast_id IS NULL AND olt_id IS NULL)"
)


def upgrade() -> None:
    kind = postgresql.ENUM(
        "pon_port",
        "wireless_sector",
        "olt_uplink",
        "bng",
        name="capacitydomainkind",
        create_type=False,
    )
    kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "capacity_domains",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", kind, nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "pon_port_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pon_ports.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "wireless_mast_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wireless_masts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "olt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("olt_devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "nas_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nas_devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("downstream_mbps", sa.Integer(), nullable=True),
        sa.Column("upstream_mbps", sa.Integer(), nullable=True),
        sa.Column(
            "target_oversubscription",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="1",
        ),
        sa.Column("capacity_source", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            _SCOPE_MATCHES_KIND, name="ck_capacity_domains_scope_matches_kind"
        ),
        sa.CheckConstraint(
            "(downstream_mbps IS NULL OR downstream_mbps > 0) "
            "AND (upstream_mbps IS NULL OR upstream_mbps > 0)",
            name="ck_capacity_domains_positive_capacity",
        ),
        sa.CheckConstraint(
            "target_oversubscription >= 1",
            name="ck_capacity_domains_oversubscription_floor",
        ),
        sa.UniqueConstraint("kind", "pon_port_id", name="uq_capacity_domains_pon"),
        sa.UniqueConstraint(
            "kind", "wireless_mast_id", name="uq_capacity_domains_sector"
        ),
        sa.UniqueConstraint("kind", "olt_id", name="uq_capacity_domains_olt"),
        sa.UniqueConstraint("kind", "nas_device_id", name="uq_capacity_domains_bng"),
    )
    op.create_index("ix_capacity_domains_kind", "capacity_domains", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_capacity_domains_kind", "capacity_domains")
    op.drop_table("capacity_domains")
    postgresql.ENUM(name="capacitydomainkind").drop(op.get_bind(), checkfirst=True)
