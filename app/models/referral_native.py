"""Native referral program ported from the CRM (Phase 3 §1.6).

CRM shapes (``dotmac_crm/app/models/crm/referral.py``) carried verbatim with
the sub conventions applied:

* PG enums become String columns + app-level enums (§1.7 vocabularies).
* Referrers/referred are customers, so the CRM person FKs re-point at sub
  ``subscribers.id`` via link keys 3/4 (**not** the staff map, §1.6):
  ``referral_codes.person_id`` → ``subscriber_id`` NOT NULL;
  ``referrals.referrer_person_id`` → ``referrer_subscriber_id`` NOT NULL;
  ``referrals.referred_person_id`` collapses with the CRM-mirror
  ``referred_subscriber_id`` into a single ``referred_subscriber_id`` FK
  (the backfill cross-checks both link paths, §3.6).
* ``ix_referrals_referrer`` and the partial unique
  ``uq_referrals_active_referred_person`` (the idempotent-capture guard) are
  recreated on the subscriber columns.
* No program table: the five ``referral_*`` program settings keys migrate
  into sub settings; ``referral_program_cache`` dies at contract (§1.6).

This module is named ``referral_native`` because ``app/models/referral.py``
still holds the ReferralMirror/ReferralProgramCache tables — both coexist
until the Phase 3 contract PR drops the mirrors (§3.3). CRM UUID PKs are
kept verbatim by the import (§3.4).
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ReferralStatus(enum.Enum):
    pending = "pending"  # captured, awaiting qualification
    qualified = "qualified"  # referred subscriber active → reward earned
    rewarded = "rewarded"  # reward issued/applied
    rejected = "rejected"  # disqualified (self-referral, fraud, etc.)
    expired = "expired"  # qualification window passed


class ReferralRewardStatus(enum.Enum):
    none = "none"  # not yet earned
    pending = "pending"  # earned, awaiting approval
    approved = "approved"  # approved, awaiting issuance/application
    issued = "issued"  # credit applied to the referrer
    void = "void"  # cancelled


class ReferralCode(Base):
    """A unique, shareable referral code owned by one referrer (subscriber)."""

    __tablename__ = "referral_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    # String(24) + the 8-char no-ambiguity alphabet minting rule ports with
    # the referrals service (§1.6).
    code: Mapped[str] = mapped_column(
        String(24), nullable=False, unique=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    referrals = relationship(
        "Referral", back_populates="code", foreign_keys="Referral.referral_code_id"
    )


class Referral(Base):
    """One attributed referral: who referred whom, its status, and the reward.

    ``metadata`` keys carried verbatim (§1.6): capture{name,email,phone},
    reward_credit_id, reward_subscriber_id.
    """

    __tablename__ = "referrals"
    __table_args__ = (
        Index("ix_referrals_referrer", "referrer_subscriber_id", "status"),
        # At most one active referral per referred subscriber (idempotent
        # capture guard, recreated from the CRM partial unique on
        # referred_person_id, §1.6).
        Index(
            "uq_referrals_active_referred_subscriber",
            "referred_subscriber_id",
            unique=True,
            postgresql_where=text("is_active AND referred_subscriber_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    referrer_subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    referral_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("referral_codes.id")
    )
    # Collapses CRM referred_person_id + referred_subscriber_id (§1.6).
    referred_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), index=True
    )
    referred_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id")
    )

    status: Mapped[str] = mapped_column(
        String(20), default=ReferralStatus.pending.value, nullable=False, index=True
    )
    reward_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reward_currency: Mapped[str] = mapped_column(String(3), default="NGN")
    reward_status: Mapped[str] = mapped_column(
        String(20), default=ReferralRewardStatus.none.value, nullable=False
    )
    reward_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    referrer = relationship("Subscriber", foreign_keys=[referrer_subscriber_id])
    referred_subscriber = relationship(
        "Subscriber", foreign_keys=[referred_subscriber_id]
    )
    code = relationship(
        "ReferralCode", back_populates="referrals", foreign_keys=[referral_code_id]
    )
    lead = relationship("Lead", foreign_keys=[referred_lead_id])
