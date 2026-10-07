"""Bounded troubleshooting access; commercial subscription state is untouched."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TestConnectionGrant(Base):
    __tablename__ = "test_connection_grants"
    __table_args__ = (
        CheckConstraint("duration_seconds > 0", name="ck_test_connection_duration"),
        CheckConstraint(
            "expires_at > activated_at", name="ck_test_connection_interval"
        ),
        CheckConstraint(
            "delivery_state IN ('pending', 'applied', 'failed')",
            name="ck_test_connection_delivery",
        ),
        Index(
            "uq_test_connection_open_subscription",
            "subscription_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
        Index("ix_test_connection_account_time", "subscriber_id", "activated_at"),
        Index("ix_test_connection_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    subscription_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscriber_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_label: Mapped[str] = mapped_column(String(160), nullable=False)
    command_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), unique=True, nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    delivery_error: Mapped[str | None] = mapped_column(String(160))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
