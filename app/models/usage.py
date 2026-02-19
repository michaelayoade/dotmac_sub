import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AccountingStatus(enum.Enum):
    start = "start"
    interim = "interim"
    stop = "stop"


class UsageSource(enum.Enum):
    radius = "radius"
    dhcp = "dhcp"
    snmp = "snmp"
    api = "api"


class UsageChargeStatus(enum.Enum):
    staged = "staged"
    posted = "posted"
    needs_review = "needs_review"
    skipped = "skipped"


class UsageRatingRunStatus(enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class QuotaBucket(Base):
    __tablename__ = "quota_buckets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    included_gb: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    used_gb: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    rollover_gb: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    overage_gb: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription", back_populates="quota_buckets")
    usage_records = relationship("UsageRecord", back_populates="quota_bucket")


class RadiusAccountingSession(Base):
    __tablename__ = "radius_accounting_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )
    access_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("access_credentials.id"), nullable=True
    )
    radius_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_clients.id")
    )
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id")
    )
    session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status_type: Mapped[AccountingStatus] = mapped_column(
        Enum(AccountingStatus), nullable=False
    )
    session_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_octets: Mapped[int | None] = mapped_column(BigInteger)
    output_octets: Mapped[int | None] = mapped_column(BigInteger)
    terminate_cause: Mapped[str | None] = mapped_column(String(120))
    splynx_session_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription")
    access_credential = relationship("AccessCredential")
    radius_client = relationship("RadiusClient")
    nas_device = relationship("NasDevice")


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    quota_bucket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quota_buckets.id")
    )
    source: Mapped[UsageSource] = mapped_column(Enum(UsageSource), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_gb: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    output_gb: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    total_gb: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription")
    quota_bucket = relationship("QuotaBucket", back_populates="usage_records")


class UsageRatingRun(Base):
    __tablename__ = "usage_rating_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[UsageRatingRunStatus] = mapped_column(
        Enum(UsageRatingRunStatus), default=UsageRatingRunStatus.running
    )
    subscriptions_scanned: Mapped[int] = mapped_column(Integer, default=0)
    charges_created: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class UsageCharge(Base):
    __tablename__ = "usage_charges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_lines.id")
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_gb: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    included_gb: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    billable_gb: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[UsageChargeStatus] = mapped_column(
        Enum(UsageChargeStatus), default=UsageChargeStatus.staged
    )
    notes: Mapped[str | None] = mapped_column(Text)
    rated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription")
    subscriber = relationship("Subscriber")
    invoice_line = relationship("InvoiceLine")
