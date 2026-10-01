"""Auditable service-period purchases and outage compensation decisions."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PrepaidPeriodPurchaseStatus(enum.Enum):
    quoted = "quoted"
    payment_pending = "payment_pending"
    completed = "completed"
    expired = "expired"
    canceled = "canceled"
    failed = "failed"


class OutageCompensationDecisionStatus(enum.Enum):
    compensated = "compensated"
    below_threshold = "below_threshold"
    excluded = "excluded"
    no_funded_overlap = "no_funded_overlap"
    review_required = "review_required"


class PrepaidPeriodPurchase(Base):
    """Immutable quote header whose children preserve per-period rounding."""

    __tablename__ = "prepaid_period_purchases"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_prepaid_period_purchase_key"
        ),
        UniqueConstraint("topup_intent_id", name="uq_prepaid_period_purchase_intent"),
        CheckConstraint(
            "period_count >= 1 AND period_count <= 12",
            name="ck_prepaid_period_purchase_count",
        ),
        CheckConstraint(
            "coverage_ends_at > coverage_starts_at",
            name="ck_prepaid_period_purchase_positive_coverage",
        ),
        Index(
            "ix_prepaid_period_purchase_subscription_status",
            "subscription_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    topup_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topup_intents.id", ondelete="RESTRICT")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT")
    )
    status: Mapped[PrepaidPeriodPurchaseStatus] = mapped_column(
        Enum(PrepaidPeriodPurchaseStatus, native_enum=False, length=24),
        nullable=False,
        default=PrepaidPeriodPurchaseStatus.quoted,
    )
    period_count: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    coverage_starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    coverage_ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    periods = relationship(
        "PrepaidPeriodPurchasePeriod",
        back_populates="purchase",
        order_by="PrepaidPeriodPurchasePeriod.ordinal",
        lazy="selectin",
    )


class PrepaidPeriodPurchasePeriod(Base):
    """One separately rounded invoice/entitlement target in a purchase."""

    __tablename__ = "prepaid_period_purchase_periods"
    __table_args__ = (
        UniqueConstraint(
            "purchase_id", "ordinal", name="uq_prepaid_purchase_period_ordinal"
        ),
        UniqueConstraint("invoice_id", name="uq_prepaid_purchase_period_invoice"),
        UniqueConstraint(
            "entitlement_id", name="uq_prepaid_purchase_period_entitlement"
        ),
        CheckConstraint(
            "ordinal >= 1 AND ordinal <= 12", name="ck_prepaid_purchase_period_ordinal"
        ),
        CheckConstraint(
            "ends_at > starts_at", name="ck_prepaid_purchase_period_positive"
        ),
        Index(
            "ix_prepaid_purchase_period_subscription", "subscription_id", "starts_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    purchase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_period_purchases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tax_application: Mapped[str] = mapped_column(String(20), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT")
    )
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_lines.id", ondelete="RESTRICT")
    )
    entitlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_entitlements.id", ondelete="RESTRICT")
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    purchase = relationship("PrepaidPeriodPurchase", back_populates="periods")


class OutageCompensationDecision(Base):
    """Append-only decision over finalized customer outage intervals."""

    __tablename__ = "outage_compensation_decisions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outage_compensation_decision_key"),
        CheckConstraint(
            "eligible_seconds >= 0", name="ck_outage_compensation_eligible_seconds"
        ),
        CheckConstraint(
            "funded_overlap_seconds >= 0", name="ck_outage_compensation_funded_seconds"
        ),
        Index(
            "ix_outage_compensation_subscription_created",
            "subscription_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[OutageCompensationDecisionStatus] = mapped_column(
        Enum(OutageCompensationDecisionStatus, native_enum=False, length=32),
        nullable=False,
    )
    threshold_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    funded_overlap_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tail_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entitlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_entitlements.id", ondelete="RESTRICT")
    )
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    intervals = relationship(
        "OutageCompensationDecisionInterval", back_populates="decision", lazy="selectin"
    )


class OutageCompensationDecisionInterval(Base):
    __tablename__ = "outage_compensation_decision_intervals"
    __table_args__ = (
        UniqueConstraint(
            "customer_outage_interval_id",
            name="uq_outage_compensation_consumed_interval",
        ),
        CheckConstraint(
            "included_seconds >= 0", name="ck_outage_compensation_included_seconds"
        ),
        CheckConstraint(
            "excluded_seconds >= 0", name="ck_outage_compensation_excluded_seconds"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outage_compensation_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_outage_interval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer_outage_intervals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    included_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    decision = relationship("OutageCompensationDecision", back_populates="intervals")
