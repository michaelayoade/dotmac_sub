"""Subscription change request model for customer self-service plan changes."""

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
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
    """Durable intent, confirmation, and result evidence for one plan change.

    Customer-reviewed requests, human-confirmed immediate changes, and admin
    next-cycle schedules share this lifecycle without sharing financial paths.
    """

    __tablename__ = "subscription_change_requests"
    __table_args__ = (
        CheckConstraint(
            "account_adjustment_id IS NULL OR credit_note_id IS NULL",
            name="ck_subscription_change_single_financial_owner",
        ),
        UniqueConstraint(
            "confirmation_idempotency_key",
            name="uq_subscription_change_confirmation_idempotency",
        ),
        Index(
            "uq_subscription_change_account_adjustment_id",
            "account_adjustment_id",
            unique=True,
        ),
        Index(
            "uq_subscription_change_credit_note_id",
            "credit_note_id",
            unique=True,
        ),
        Index(
            "uq_subscription_change_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
    )

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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    # Immediate human-confirmed changes bind the displayed owner preview to the
    # committed request. Historical and scheduled next-cycle rows remain NULL:
    # those paths either predate the contract or produce no immediate money.
    confirmation_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    confirmation_idempotency_key: Mapped[str | None] = mapped_column(String(120))
    confirmation_origin: Mapped[str | None] = mapped_column(String(40))
    confirmation_snapshot: Mapped[dict | None] = mapped_column(JSON)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Structural links to the exact owner result. At most one document owner is
    # present; ledger_entry_id names the exact resulting transaction for either
    # the debit adjustment or credit note. A zero-money change has none.
    account_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_adjustments.id", ondelete="RESTRICT"),
    )
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("credit_notes.id", ondelete="RESTRICT"),
    )
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscription = relationship("Subscription", foreign_keys=[subscription_id])
    current_offer = relationship("CatalogOffer", foreign_keys=[current_offer_id])
    requested_offer = relationship("CatalogOffer", foreign_keys=[requested_offer_id])
    requested_by = relationship("Subscriber", foreign_keys=[requested_by_subscriber_id])
    reviewed_by = relationship("Subscriber", foreign_keys=[reviewed_by_subscriber_id])
    account_adjustment = relationship(
        "AccountAdjustment", foreign_keys=[account_adjustment_id]
    )
    credit_note = relationship("CreditNote", foreign_keys=[credit_note_id])
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
