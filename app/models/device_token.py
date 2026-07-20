"""Per-device push tokens (FCM/APNs) registered by the mobile app.

The notification delivery worker resolves a subscriber's active tokens to send
mobile push. Transport is config-gated (see app/services/push.py): without FCM
credentials the rows are still stored, so push 'lights up' once creds exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # The opaque FCM/APNs registration token. Unique so re-registration from the
    # same device upserts rather than duplicating.
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    platform: Mapped[str | None] = mapped_column(String(16))  # ios | android | web
    app_version: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
