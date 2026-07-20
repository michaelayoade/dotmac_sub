"""Retained historical CRM referral mirror rows (RFC #73).

Native referral tables are authoritative. These legacy rows are read-only
migration/retention evidence: no customer surface, webhook, task, or service
hydrates them or derives a native referral decision from them.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.db import Base


class ReferralMirror(Base):
    """One retained historical CRM referral snapshot."""

    __tablename__ = "referral_mirror"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_referral_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    referred_name: Mapped[str | None] = mapped_column(String(160))
    # pending | qualified | rewarded | rejected (CRM ReferralStatus values).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reward_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reward_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="NGN"
    )
    # none | pending | approved | paid (CRM ReferralRewardStatus values).
    reward_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none"
    )
    referral_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ReferralProgramCache(Base):
    """Retained historical per-subscriber referral program snapshot."""

    __tablename__ = "referral_program_cache"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    share_url: Mapped[str] = mapped_column(String(255), nullable=False)
    program_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    reward_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reward_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="NGN"
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
