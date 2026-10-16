"""Configuration for annual customer greetings; delivery lives in the intent outbox."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CustomerOccasionAutomation(Base):
    __tablename__ = "customer_occasion_automations"
    __table_args__ = (
        CheckConstraint(
            "occasion IN ('birthday', 'anniversary')", name="ck_occasion_kind"
        ),
        CheckConstraint("send_hour BETWEEN 0 AND 23", name="ck_occasion_hour"),
        CheckConstraint("send_minute BETWEEN 0 AND 59", name="ck_occasion_minute"),
        CheckConstraint("revision > 0", name="ck_occasion_revision"),
    )

    occasion: Mapped[str] = mapped_column(String(20), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(80), default="Africa/Lagos", nullable=False
    )
    send_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    send_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
