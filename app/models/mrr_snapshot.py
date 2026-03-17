"""MRR (Monthly Recurring Revenue) historical snapshots."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class MrrSnapshot(Base):
    """Daily MRR snapshot per subscriber.

    Populated by a nightly Celery task that iterates active subscribers
    and sums their subscription MRR.  Used for revenue trend reporting,
    churn analysis, and Splynx mrr_statistics migration (2.99M rows).
    """

    __tablename__ = "mrr_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id",
            "snapshot_date",
            name="uq_mrr_snapshot_subscriber_date",
        ),
        Index("ix_mrr_snapshots_date", "snapshot_date"),
        Index("ix_mrr_snapshots_subscriber", "subscriber_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    mrr_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    active_subscriptions: Mapped[int] = mapped_column(Integer, default=0)

    # Migration mapping
    splynx_customer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscriber = relationship("Subscriber")
