import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IntegrationConnectorType(enum.Enum):
    payment = "payment"
    accounting = "accounting"
    messaging = "messaging"
    network = "network"
    crm = "crm"
    voice = "voice"
    custom = "custom"


class IntegrationConnectorStatus(enum.Enum):
    enabled = "enabled"
    disabled = "disabled"
    not_installed = "not_installed"


class IntegrationConnector(Base):
    __tablename__ = "integration_connectors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    connector_type: Mapped[IntegrationConnectorType] = mapped_column(
        Enum(IntegrationConnectorType), default=IntegrationConnectorType.custom
    )
    status: Mapped[IntegrationConnectorStatus] = mapped_column(
        Enum(IntegrationConnectorStatus),
        default=IntegrationConnectorStatus.not_installed,
    )
    configuration: Mapped[dict | None] = mapped_column(JSON)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
