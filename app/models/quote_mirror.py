"""Local mirror of CRM self-serve quote data (Sales/Quotes tracker).

The CRM owns quotes; these tables are a read-optimised local copy so the
customer app/web can show a quote (feasibility, estimate, deposit, status)
instantly and during a CRM outage. Hydrated by CRM ``quote.*`` webhooks + a
periodic reconcile pull + the write-through that requests a quote. Mirrors the
work-order/project mirror design.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.db import Base


class QuoteMirror(Base):
    """One CRM self-serve quote for one of our subscribers (local copy)."""

    __tablename__ = "quote_mirror"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_quote_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # draft|sent|accepted|rejected|expired (CRM QuoteStatus)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    total: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    deposit_amount: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    deposit_percent: Mapped[int | None] = mapped_column()
    deposit_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # covered|survey_required|out_of_area
    feasibility_coverage: Mapped[str | None] = mapped_column(String(20))
    estimate_provisional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    address: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    project_id: Mapped[str | None] = mapped_column(String(64))
    sales_order_id: Mapped[str | None] = mapped_column(String(64))
    # Full CRM portal payload (line items, feasibility detail) for rich rendering.
    payload: Mapped[dict | None] = mapped_column(JSON)
    quote_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class QuoteSyncState(Base):
    """Per-subscriber reconcile marker — drives the lazy on-view refresh TTL even
    when the subscriber has zero quotes."""

    __tablename__ = "quote_sync_state"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
