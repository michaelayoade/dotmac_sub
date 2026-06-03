"""Admin dashboard What's New slide items."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AdminWhatsNewItem(Base):
    """Content items shown in the admin dashboard What's New carousel."""

    __tablename__ = "admin_whats_new_items"
    __table_args__ = (
        Index("ix_admin_whats_new_items_status", "status"),
        Index("ix_admin_whats_new_items_starts_at", "starts_at"),
        Index("ix_admin_whats_new_items_ends_at", "ends_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    benefit_one: Mapped[str | None] = mapped_column(String(255))
    benefit_two: Mapped[str | None] = mapped_column(String(255))
    benefit_three: Mapped[str | None] = mapped_column(String(255))
    button_text: Mapped[str] = mapped_column(String(80), nullable=False)
    button_link: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<AdminWhatsNewItem {self.status}: {self.title}>"
