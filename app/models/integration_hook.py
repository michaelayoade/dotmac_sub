import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class IntegrationHookType(enum.Enum):
    web = "web"
    cli = "cli"
    internal = "internal"


class IntegrationHookAuthType(enum.Enum):
    none = "none"
    bearer = "bearer"
    basic = "basic"
    hmac = "hmac"


class IntegrationHookExecutionStatus(enum.Enum):
    success = "success"
    failed = "failed"


class IntegrationHook(Base):
    __tablename__ = "integration_hooks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    hook_type: Mapped[IntegrationHookType] = mapped_column(
        Enum(IntegrationHookType), default=IntegrationHookType.web, nullable=False
    )
    command: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(600))
    http_method: Mapped[str] = mapped_column(String(10), default="POST", nullable=False)
    auth_type: Mapped[IntegrationHookAuthType] = mapped_column(
        Enum(IntegrationHookAuthType), default=IntegrationHookAuthType.none, nullable=False
    )
    auth_config: Mapped[dict | None] = mapped_column(JSON)
    retry_max: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    retry_backoff_ms: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    event_filters: Mapped[list[str] | None] = mapped_column(JSON)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    executions = relationship(
        "IntegrationHookExecution",
        back_populates="hook",
        cascade="all, delete-orphan",
    )


class IntegrationHookExecution(Base):
    __tablename__ = "integration_hook_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    hook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_hooks.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[IntegrationHookExecutionStatus] = mapped_column(
        Enum(IntegrationHookExecutionStatus),
        default=IntegrationHookExecutionStatus.success,
        nullable=False,
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    response_status: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict | None] = mapped_column(JSON)
    response_body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    hook = relationship("IntegrationHook", back_populates="executions")

