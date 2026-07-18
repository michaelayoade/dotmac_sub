"""When a subscriber last dismissed the "confirm your location" prompt.

One row per subscriber. It records a snooze — "remind me later" — not a
refusal: the prompt returns once the snooze lapses, and payment always
overrides it (a confirmed service location is a regulatory obligation, not a
marketing nag, so it is not permanently dismissible).

This is deliberately NOT ``portal_onboarding_states``. That table is a dead
Splynx import artifact (zero rows, unwired, shaped as a step counter); reusing
it would resurrect a retired thing to hold an unrelated fact.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LocationCapturePromptState(Base):
    """A subscriber's snooze state for the location-confirmation prompt."""

    __tablename__ = "location_capture_prompt_states"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # The prompt stays hidden until this moment. Set when the customer clicks
    # "remind me later"; payment re-prompts regardless of it.
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismiss_count: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
