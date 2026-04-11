"""Splynx ID mapping — bidirectional integer↔UUID lookup for migration."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SplynxEntityType(enum.Enum):
    customer = "customer"
    service = "service"
    tariff = "tariff"
    invoice = "invoice"
    payment = "payment"
    transaction = "transaction"
    credit_note = "credit_note"
    ticket = "ticket"
    quote = "quote"
    router = "router"
    location = "location"
    partner = "partner"
    email = "email"
    sms = "sms"
    scheduling_task = "scheduling_task"
    inventory_item = "inventory_item"
    ip_network = "ip_network"
    radius_profile = "radius_profile"


class SplynxIdMapping(Base):
    """Bidirectional mapping between Splynx integer IDs and DotMac UUIDs.

    Used during and after migration to correlate records across systems.
    Covers entities that don't have a dedicated ``splynx_*_id`` column
    on their primary model.
    """

    __tablename__ = "splynx_id_mappings"
    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "splynx_id",
            name="uq_splynx_mapping_type_splynx_id",
        ),
        UniqueConstraint(
            "entity_type",
            "dotmac_id",
            name="uq_splynx_mapping_type_dotmac_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[SplynxEntityType] = mapped_column(
        Enum(
            SplynxEntityType,
            name="splynxentitytype",
            create_constraint=False,
        ),
        nullable=False,
    )
    splynx_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dotmac_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    migrated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Extra context from migration (e.g. source table, batch id)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
