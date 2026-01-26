import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ConnectorType(enum.Enum):
    webhook = "webhook"
    http = "http"
    email = "email"
    whatsapp = "whatsapp"
    smtp = "smtp"
    stripe = "stripe"
    twilio = "twilio"
    facebook = "facebook"
    instagram = "instagram"
    custom = "custom"


class ConnectorAuthType(enum.Enum):
    none = "none"
    basic = "basic"
    bearer = "bearer"
    hmac = "hmac"
    api_key = "api_key"
    oauth2 = "oauth2"


class ConnectorConfig(Base):
    __tablename__ = "connector_configs"
    __table_args__ = (UniqueConstraint("name", name="uq_connector_configs_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    connector_type: Mapped[ConnectorType] = mapped_column(
        Enum(ConnectorType), default=ConnectorType.custom
    )
    base_url: Mapped[str | None] = mapped_column(String(500))
    auth_type: Mapped[ConnectorAuthType] = mapped_column(
        Enum(ConnectorAuthType), default=ConnectorAuthType.none
    )
    auth_config: Mapped[dict | None] = mapped_column(JSON)
    headers: Mapped[dict | None] = mapped_column(JSON)
    retry_policy: Mapped[dict | None] = mapped_column(JSON)
    timeout_sec: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
