"""Immutable evidence for reviewed Paystack outside-window recovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PaystackOutsideWindowRecoveryRun(Base):
    """One exact, fingerprint-bound finance recovery result."""

    __tablename__ = "paystack_outside_window_recovery_runs"
    __table_args__ = (
        Index(
            "uq_paystack_outside_window_recovery_idempotency",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_paystack_outside_window_recovery_intent_created",
            "intent_id",
            "created_at",
        ),
        CheckConstraint(
            "provider_type = 'paystack'",
            name="ck_paystack_outside_window_recovery_provider",
        ),
        CheckConstraint(
            "disposition IN ('recovered', 'linked')",
            name="ck_paystack_outside_window_recovery_disposition",
        ),
        CheckConstraint(
            "gross_amount > 0 AND provider_fee >= 0 "
            "AND provider_fee <= gross_amount "
            "AND authorized_net_amount > 0 "
            "AND authorized_net_amount <= gross_amount",
            name="ck_paystack_outside_window_recovery_money",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topup_intents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_provider_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    checkout_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_capability_bindings.id", ondelete="RESTRICT"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    review_reference: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    provider_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    authorized_net_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    disposition: Mapped[str] = mapped_column(String(24), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
