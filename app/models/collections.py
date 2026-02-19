import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.catalog import DunningAction, PolicySet


class DunningCaseStatus(enum.Enum):
    open = "open"
    paused = "paused"
    resolved = "resolved"
    closed = "closed"


class DunningCase(Base):
    __tablename__ = "dunning_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        "subscriber_id", UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    status: Mapped[DunningCaseStatus] = mapped_column(
        Enum(DunningCaseStatus), default=DunningCaseStatus.open
    )
    current_step: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    subscriber = relationship("Subscriber", back_populates="dunning_cases", foreign_keys=[account_id])
    policy_set = relationship("PolicySet")
    actions = relationship("DunningActionLog", back_populates="case")


class DunningActionLog(Base):
    __tablename__ = "dunning_action_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dunning_cases.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    step_day: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[DunningAction] = mapped_column(Enum(DunningAction), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    case = relationship("DunningCase", back_populates="actions")
    invoice = relationship("Invoice", back_populates="dunning_actions")
    payment = relationship("Payment", back_populates="dunning_actions")
