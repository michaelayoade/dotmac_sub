"""Autopay mandate — opt-in to auto-charge a saved card on due invoices.

Kept in its own table (not a column on payment_methods/subscribers) so it is
fully isolated: existing queries are unaffected, and the feature simply has no
rows until the migration is applied in a given environment.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AutopayMandate(Base):
    __tablename__ = "autopay_mandates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id"),
        nullable=False,
        unique=True,
    )
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_methods.id")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Decline tracking: consecutive failed charge runs. Reset on a successful
    # charge, on re-enable, or when the customer picks a new default card.
    # Mandates at/over the failure cap are skipped by the charge engine.
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    payment_method = relationship("PaymentMethod")
