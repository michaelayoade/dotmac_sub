"""Evidence that a subscriber field was *confirmed* — not merely present.

An append-only ledger. Each row says: someone confirmed this field held this
value, at this time, from this source, with this evidence. Nothing else in the
schema can answer that question: a populated column tells you a value exists,
not that anyone ever checked it.

That distinction is the whole point. Of the 4,054 subscriber locations Sub
files to the NCC, 3,558 were *inferred* by matching address text against a
place gazetteer and 496 were absent — zero are confirmed facts. A column
cannot hold "who said so"; this table can.

Append-only by convention: capture writes a new row rather than mutating an
old one, so a field's confirmation history survives (a customer who moves is
re-confirmed, not overwritten). The latest row per (subscriber, field) is the
current confirmation; older rows are the audit trail.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SubscriberFieldVerification(Base):
    """One confirmation of one subscriber field."""

    __tablename__ = "subscriber_field_verifications"
    __table_args__ = (
        Index(
            "ix_subscriber_field_verifications_subscriber_field",
            "subscriber_id",
            "field_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Values are declared by
    # app.services.subscriber_data_completeness.FieldKey.
    field_key: Mapped[str] = mapped_column(String(40), nullable=False)
    # What was confirmed, as confirmed. Kept verbatim so a later policy change
    # cannot retroactively reinterpret what the customer actually said.
    value: Mapped[str | None] = mapped_column(String(255))
    # How it was confirmed: customer_portal / field_gps / agent / import.
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Who confirmed it — mirrors customer_location_change_requests' reviewer
    # columns. Null for unattended sources (a device GPS fix has no actor).
    verified_by_actor_id: Mapped[str | None] = mapped_column(String(120))
    verified_by_actor_name: Mapped[str | None] = mapped_column(String(200))
    # Source-specific proof, e.g. {"lat":.., "lng":.., "accuracy_m":..} for a
    # GPS capture. A coarse fix is still evidence — but the accuracy travels
    # with it, so a consumer can refuse to derive an LGA from a 500m fix
    # rather than silently pretending to precision.
    evidence: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
