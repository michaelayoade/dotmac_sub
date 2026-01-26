"""Subscription change request model for customer self-service plan changes."""

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SubscriptionChangeStatus(enum.Enum):
    """Status for subscription change requests."""
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    applied = "applied"
    canceled = "canceled"


class SubscriptionChangeRequest(Base):
    """Customer request to change their subscription plan.

    Allows customers to request plan upgrades/downgrades through the portal.
    Requests can be reviewed and approved by administrators.
    """
    __tablename__ = "subscription_change_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    current_offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    requested_offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("catalog_offers.id"), nullable=False
    )
    status: Mapped[SubscriptionChangeStatus] = mapped_column(
        Enum(SubscriptionChangeStatus), default=SubscriptionChangeStatus.pending
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    requested_by_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    reviewed_by_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    subscription = relationship("Subscription", foreign_keys=[subscription_id])
    current_offer = relationship("CatalogOffer", foreign_keys=[current_offer_id])
    requested_offer = relationship("CatalogOffer", foreign_keys=[requested_offer_id])
    requested_by = relationship("Subscriber", foreign_keys=[requested_by_subscriber_id])
    reviewed_by = relationship("Subscriber", foreign_keys=[reviewed_by_subscriber_id])
