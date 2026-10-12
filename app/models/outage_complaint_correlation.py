"""Native immutable receipt for the reviewed complaint correlation owner."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OutageComplaintCorrelationReceipt(Base):
    __tablename__ = "outage_complaint_correlation_receipts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incidents.id", ondelete="RESTRICT"), nullable=False
    )
    intent_id: Mapped[UUID] = mapped_column(nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    link_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incident_ticket_links.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    original_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_scope_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proof_origin: Mapped[str] = mapped_column(String(24), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("incident_id", "intent_id"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(trim(reason))>0 AND length(ref_id)=113 AND proof_origin IN ('new_link','reviewed_adoption')"
        ),
    )
