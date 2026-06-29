"""Local mirror of CRM project/installation data (Installation tracker).

The CRM owns projects; these tables are a read-optimised local copy so the
customer app/web can show "where's my install?" instantly and during a CRM
outage. Hydrated by CRM ``project.*`` webhooks + a periodic reconcile pull.
Mirrors the ``referral`` mirror design.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.db import Base


class ProjectMirror(Base):
    """One CRM project attributed to one of our subscribers (local copy)."""

    __tablename__ = "project_mirror"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_project_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # open | planned | active | on_hold | completed | canceled (CRM ProjectStatus)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    project_type: Mapped[str | None] = mapped_column(String(60))
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_stage: Mapped[str | None] = mapped_column(String(120))
    # Ordered timeline: [{key, title, status, completed_at}].
    stages: Mapped[list | None] = mapped_column(JSONB)
    customer_address: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(80))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    project_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ProjectSyncState(Base):
    """Per-subscriber reconcile marker — drives the lazy on-view refresh TTL even
    when the subscriber has zero projects."""

    __tablename__ = "project_sync_state"

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
