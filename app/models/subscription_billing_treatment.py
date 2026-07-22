"""Authoritative non-standard billing treatment and exact service-grant evidence."""

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
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.catalog import BillingCycle


class SubscriptionBillingTreatment(enum.Enum):
    standard = "standard"
    complimentary = "complimentary"
    sponsored = "sponsored"


class BillingTreatmentReason(enum.Enum):
    internal_service = "internal_service"
    staff_benefit = "staff_benefit"
    partner_service = "partner_service"
    community_support = "community_support"
    commercial_concession = "commercial_concession"
    sponsored_service = "sponsored_service"
    other_approved = "other_approved"


class BillingTreatmentStatus(enum.Enum):
    active = "active"
    revoked = "revoked"


class SubscriptionBillingArrangement(Base):
    """Effective-dated approval not to bill one subscription's customer."""

    __tablename__ = "subscription_billing_arrangements"
    __table_args__ = (
        CheckConstraint(
            "treatment IN ('complimentary', 'sponsored')",
            name="ck_subscription_billing_arrangement_nonstandard",
        ),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_subscription_billing_arrangement_period",
        ),
        CheckConstraint(
            "maximum_recurring_amount > 0",
            name="ck_subscription_billing_arrangement_positive_value",
        ),
        CheckConstraint(
            "approval_policy_max_days BETWEEN 1 AND 366",
            name="ck_subscription_billing_arrangement_approval_policy",
        ),
        CheckConstraint(
            "treatment <> 'sponsored' OR "
            "sponsor_reference IS NOT NULL OR cost_center IS NOT NULL",
            name="ck_subscription_billing_arrangement_sponsor_evidence",
        ),
        UniqueConstraint(
            "subscription_id",
            "starts_at",
            name="uq_subscription_billing_arrangement_start",
        ),
        Index(
            "ix_subscription_billing_arrangements_effective",
            "subscription_id",
            "status",
            "starts_at",
            "ends_at",
        ),
        Index(
            "uq_subscription_billing_arrangements_idempotency",
            "idempotency_key_sha256",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorized_offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    treatment: Mapped[SubscriptionBillingTreatment] = mapped_column(
        Enum(SubscriptionBillingTreatment, name="subscription_billing_treatment"),
        nullable=False,
    )
    reason_code: Mapped[BillingTreatmentReason] = mapped_column(
        Enum(BillingTreatmentReason, name="billing_treatment_reason"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approval_policy_max_days: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_recurring_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        Enum(BillingCycle), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    sponsor_reference: Mapped[str | None] = mapped_column(String(200))
    cost_center: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[BillingTreatmentStatus] = mapped_column(
        Enum(BillingTreatmentStatus, name="billing_treatment_status"),
        nullable=False,
        default=BillingTreatmentStatus.active,
    )
    approved_by: Mapped[str] = mapped_column(String(120), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_by: Mapped[str | None] = mapped_column(String(120))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    revocation_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True
    )
    revocation_correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    revocation_idempotency_key_sha256: Mapped[str | None] = mapped_column(
        String(64), unique=True
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    idempotency_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscription = relationship("Subscription")
    account = relationship("Subscriber")
    authorized_offer = relationship("CatalogOffer")
    grants = relationship(
        "SubscriptionBillingGrant",
        back_populates="arrangement",
        order_by="SubscriptionBillingGrant.starts_at",
    )


class SubscriptionBillingGrant(Base):
    """Append-only exact non-cash evidence for one approved service period."""

    __tablename__ = "subscription_billing_grants"
    __table_args__ = (
        CheckConstraint(
            "treatment IN ('complimentary', 'sponsored')",
            name="ck_subscription_billing_grant_nonstandard",
        ),
        CheckConstraint(
            "ends_at > starts_at", name="ck_subscription_billing_grant_period"
        ),
        CheckConstraint(
            "reference_amount > 0",
            name="ck_subscription_billing_grant_positive_value",
        ),
        UniqueConstraint(
            "arrangement_id",
            "starts_at",
            "ends_at",
            name="uq_subscription_billing_grant_period",
        ),
        Index(
            "uq_subscription_billing_grants_idempotency",
            "idempotency_key_sha256",
            unique=True,
        ),
        Index(
            "ix_subscription_billing_grants_subscription_period",
            "subscription_id",
            "starts_at",
            "ends_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    arrangement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_billing_arrangements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    treatment: Mapped[SubscriptionBillingTreatment] = mapped_column(
        Enum(SubscriptionBillingTreatment, name="subscription_billing_treatment"),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    idempotency_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    arrangement = relationship(
        "SubscriptionBillingArrangement", back_populates="grants"
    )
    subscription = relationship("Subscription")
    account = relationship("Subscriber")
    entitlement = relationship(
        "ServiceEntitlement", back_populates="source_billing_grant", uselist=False
    )
