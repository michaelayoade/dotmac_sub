import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
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
    __table_args__ = (
        Index(
            "ix_quota_buckets_subscription_id_period_start",
            "subscription_id",
            "period_start",
        ),
    )

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
    # GB granted by data top-up purchases this period (counts toward the
    # allowance before overage, alongside included + rollover).
    topup_gb: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    overage_gb: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscription = relationship("Subscription", back_populates="quota_buckets")
    usage_records = relationship("UsageRecord", back_populates="quota_bucket")


class RadiusAccountingSession(Base):
    __tablename__ = "radius_accounting_sessions"
    __table_args__ = (
        Index(
            "ix_radius_accounting_sessions_subscription_id",
            "subscription_id",
        ),
        Index(
            "ix_radius_accounting_sessions_credential_session",
            "access_credential_id",
            "session_id",
        ),
    )

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
    # Last accounting observation (acctupdatetime / acctstoptime). A live
    # session keeps advancing this via interim updates; an open session whose
    # last_update_at goes stale is a ghost and gets reaped.
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the importer's refresh pass last re-read this session from radacct.
    # Round-robin key: least-recently-attempted first, so unchanging ghosts
    # can't pin the refresh window.
    refresh_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    input_octets: Mapped[int | None] = mapped_column(BigInteger)
    output_octets: Mapped[int | None] = mapped_column(BigInteger)
    terminate_cause: Mapped[str | None] = mapped_column(String(120))
    # Framed addresses from radacct. v4 populates wherever FreeRADIUS logs
    # accounting; the v6 columns fill only if the NAS sends them AND
    # queries.conf writes them — absent columns are skipped at import.
    framed_ip_address: Mapped[str | None] = mapped_column(String(64))
    framed_ipv6_prefix: Mapped[str | None] = mapped_column(String(128))
    delegated_ipv6_prefix: Mapped[str | None] = mapped_column(String(128))
    # Physical attachment from radacct: NAS-Port-Id (e.g. PPPoE interface) and
    # Called-Station-Id — enables per-port concurrency / fault correlation.
    nas_port_id: Mapped[str | None] = mapped_column(String(64))
    called_station_id: Mapped[str | None] = mapped_column(String(64))
    splynx_session_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription")
    access_credential = relationship("AccessCredential")
    radius_client = relationship("RadiusClient")
    nas_device = relationship("NasDevice")


class SubscriberDailyUsage(Base):
    """Daily upload/download volume per subscription.

    Imported from Splynx ``traffic_counter`` (one row per service per day,
    history back to 2018). This is the long-history daily rollup — distinct
    from :class:`RadiusAccountingSession` (per-session detail, 2023+) and
    :class:`QuotaBucket` (per-billing-cycle). ``splynx_service_id`` +
    ``usage_date`` is the natural key from the source table, used for an
    idempotent re-runnable import.
    """

    __tablename__ = "subscriber_daily_usage"
    __table_args__ = (
        UniqueConstraint(
            "splynx_service_id",
            "usage_date",
            name="uq_subscriber_daily_usage_service_date",
        ),
        Index(
            "ix_subscriber_daily_usage_subscription_date",
            "subscription_id",
            "usage_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )
    # Source service id from Splynx (traffic_counter.service_id). Retained for
    # traceability and as the idempotency key even when a subscription mapping
    # is missing (deleted service).
    splynx_service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    upload_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    download_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    source: Mapped[str] = mapped_column(
        String(40), default="splynx_traffic_counter", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscription = relationship("Subscription")


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index(
            "ix_usage_records_subscription_id_recorded_at",
            "subscription_id",
            "recorded_at",
        ),
    )

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
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscription = relationship("Subscription")
    subscriber = relationship("Subscriber")
    invoice_line = relationship("InvoiceLine")
