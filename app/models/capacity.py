"""Shared-capacity segments and what has been sold behind them.

A subscriber's experience is bounded by the narrowest shared segment their
traffic crosses — the PON port, the wireless sector, the OLT uplink, the BNG.
Those are different hardware but the same idea: a bandwidth ceiling with a set
of subscribers behind it. Modelling them separately would mean three capacity
checks that disagree, so they share one table and one resolver.

This exists because contention was an integer on a catalogue offer that nothing
enforced and nothing measured. A ratio written on a plan commits nothing; a
capacity figure attached to the segment it describes can be compared against
what has actually been sold.

Capacity is recorded, never inferred. A GPON port is *usually* 2.488 G down and
1.244 G up, but split ratio, XGS-PON upgrades and shared uplinks all change the
real number, and guessing produces a check that quietly passes.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CapacityDomainKind(enum.Enum):
    """What kind of shared segment this is.

    Each kind populates exactly one scope column, enforced in the database, so
    a domain cannot claim to be two things and the resolver never has to guess
    which parent to follow.
    """

    #: One PON port on an OLT — the first shared segment for fibre subscribers.
    pon_port = "pon_port"
    #: One sector/radio on a mast — the equivalent for fixed wireless.
    wireless_sector = "wireless_sector"
    #: An OLT's upstream link, shared by every PON port on it.
    olt_uplink = "olt_uplink"
    #: A BNG/NAS forwarding budget, shared across everything it terminates.
    bng = "bng"


class CapacityDomain(Base):
    """One shared segment, its provisioned capacity, and its planning target.

    ``downstream_mbps``/``upstream_mbps`` are what the segment can actually
    carry. ``target_oversubscription`` is how much may be sold against it — 1
    means 1:1 with no oversubscription, 5 means five times capacity may be
    sold. Both are recorded by capacity planning, not derived from the
    catalogue: the catalogue states an intent, this states the physical fact.
    """

    __tablename__ = "capacity_domains"
    __table_args__ = (
        # Exactly one scope, matching the kind. Without this a domain could
        # name a PON port while claiming to be a BNG, and the resolver would
        # aggregate the same subscribers twice.
        CheckConstraint(
            "(kind = 'pon_port' AND pon_port_id IS NOT NULL "
            " AND wireless_mast_id IS NULL AND olt_id IS NULL "
            " AND nas_device_id IS NULL) "
            "OR (kind = 'wireless_sector' AND wireless_mast_id IS NOT NULL "
            " AND pon_port_id IS NULL AND olt_id IS NULL "
            " AND nas_device_id IS NULL) "
            "OR (kind = 'olt_uplink' AND olt_id IS NOT NULL "
            " AND pon_port_id IS NULL AND wireless_mast_id IS NULL "
            " AND nas_device_id IS NULL) "
            "OR (kind = 'bng' AND nas_device_id IS NOT NULL "
            " AND pon_port_id IS NULL AND wireless_mast_id IS NULL "
            " AND olt_id IS NULL)",
            name="ck_capacity_domains_scope_matches_kind",
        ),
        # Capacity is NULLABLE on purpose: a segment must be nameable before it
        # is surveyed, or there is no way to enumerate what still needs
        # measuring. A NULL reads as unknown and never as healthy. Zero or
        # negative is still refused — that is a bad measurement, not a missing
        # one, and the two must not look alike.
        CheckConstraint(
            "(downstream_mbps IS NULL OR downstream_mbps > 0) "
            "AND (upstream_mbps IS NULL OR upstream_mbps > 0)",
            name="ck_capacity_domains_positive_capacity",
        ),
        # 1 means 1:1 — the strictest a segment can be. Zero or negative would
        # make the headroom arithmetic meaningless rather than merely strict.
        CheckConstraint(
            "target_oversubscription >= 1",
            name="ck_capacity_domains_oversubscription_floor",
        ),
        UniqueConstraint("kind", "pon_port_id", name="uq_capacity_domains_pon"),
        UniqueConstraint("kind", "wireless_mast_id", name="uq_capacity_domains_sector"),
        UniqueConstraint("kind", "olt_id", name="uq_capacity_domains_olt"),
        UniqueConstraint("kind", "nas_device_id", name="uq_capacity_domains_bng"),
        Index("ix_capacity_domains_kind", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[CapacityDomainKind] = mapped_column(
        Enum(CapacityDomainKind, name="capacitydomainkind", create_constraint=False),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id", ondelete="CASCADE")
    )
    wireless_mast_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wireless_masts.id", ondelete="CASCADE")
    )
    olt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id", ondelete="CASCADE")
    )
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id", ondelete="CASCADE")
    )

    #: NULL until surveyed. See the constraint above for why this is nullable.
    downstream_mbps: Mapped[int | None] = mapped_column(Integer)
    upstream_mbps: Mapped[int | None] = mapped_column(Integer)
    #: How much may be sold against this segment, as a multiple of capacity.
    target_oversubscription: Mapped[float] = mapped_column(
        Numeric(6, 2), nullable=False, default=1
    )
    #: Where the capacity figure came from — a survey, a vendor sheet, an
    #: uplink config. A number with no provenance gets trusted for years.
    capacity_source: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pon_port = relationship("PonPort")
    wireless_mast = relationship("WirelessMast")
    olt = relationship("OLTDevice")
    nas_device = relationship("NasDevice")
