import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SlaCreditReportStatus(enum.Enum):
    draft = "draft"
    reviewed = "reviewed"
    applied = "applied"
    canceled = "canceled"


class SlaCreditReport(Base):
    __tablename__ = "sla_credit_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlaCreditReportStatus] = mapped_column(
        Enum(SlaCreditReportStatus), default=SlaCreditReportStatus.draft
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = relationship("SlaCreditItem", back_populates="report")


class SlaCreditItem(Base):
    __tablename__ = "sla_credit_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_credit_reports.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    sla_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_profiles.id")
    )
    target_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("100.00"))
    actual_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("100.00"))
    credit_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"))
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    memo: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    report = relationship("SlaCreditReport", back_populates="items")
    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
    invoice = relationship("Invoice")
    sla_profile = relationship("SlaProfile")
