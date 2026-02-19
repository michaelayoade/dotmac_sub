import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ExternalEntityType(enum.Enum):
    """Entity types that can be synced with external systems."""
    subscriber = "subscriber"
    subscription = "subscription"
    invoice = "invoice"
    service_order = "service_order"
    # Backwards-compat: older integrations/tests used ticket references.
    ticket = "ticket"


class ExternalReference(Base):
    __tablename__ = "external_references"
    __table_args__ = (
        UniqueConstraint(
            "connector_config_id",
            "entity_type",
            "entity_id",
            name="uq_external_refs_connector_entity",
        ),
        UniqueConstraint(
            "connector_config_id",
            "entity_type",
            "external_id",
            name="uq_external_refs_connector_external",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    entity_type: Mapped[ExternalEntityType] = mapped_column(
        Enum(ExternalEntityType), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    external_url: Mapped[str | None] = mapped_column(String(500))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    connector_config = relationship("ConnectorConfig")
