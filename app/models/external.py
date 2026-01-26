import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ExternalEntityType(enum.Enum):
    ticket = "ticket"
    ticket_comment = "ticket_comment"
    project = "project"
    project_task = "project_task"
    work_order = "work_order"
    work_order_note = "work_order_note"


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
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    connector_config = relationship("ConnectorConfig")
