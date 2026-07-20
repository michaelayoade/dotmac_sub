"""Native referral program ported from CRM.

CRM shapes (``dotmac_crm/app/models/crm/referral.py``) carried verbatim with
the sub conventions applied:

* PG enums become String columns + app-level enums.
* Referrers are customers, so the CRM person FK re-points at sub
  ``subscribers.id`` via link keys 3/4 (**not** the staff map, ):
  ``referral_codes.person_id`` → ``subscriber_id`` NOT NULL;
  ``referrals.referrer_person_id`` → ``referrer_subscriber_id`` NOT NULL;
  legacy referred accounts remain in ``referred_subscriber_id`` while new
  capture binds ``referred_party_id`` before an account exists.
* ``ix_referrals_referrer`` and the partial unique
  ``uq_referrals_active_referred_person`` (the idempotent-capture guard) are
  recreated on the subscriber columns.
* No program table: the five ``referral_*`` program settings keys migrate
  into sub settings; ``referral_program_cache`` dies at contract.

This module is named ``referral_native`` because ``app/models/referral.py``
still holds the ReferralMirror/ReferralProgramCache tables — both coexist
until verified native ownership permits the mirrors to be dropped. CRM UUID PKs are
kept verbatim by the import.
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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
    # the referrals service.
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

    ``metadata`` keeps reward evidence. New capture contact PII belongs to the
    referred Party's contact points; legacy ``capture`` metadata remains
    readable until its reviewed cleanup.
    """

    __tablename__ = "referrals"
    __table_args__ = (
        Index("ix_referrals_referrer", "referrer_subscriber_id", "status"),
        Index(
            "uq_referrals_active_referred_party",
            "referred_party_id",
            unique=True,
            postgresql_where=text("is_active AND referred_party_id IS NOT NULL"),
        ),
        # At most one active referral per referred subscriber (idempotent
        # capture guard, recreated from the CRM partial unique on
        # referred_person_id, ).
        Index(
            "uq_referrals_active_referred_subscriber",
            "referred_subscriber_id",
            unique=True,
            postgresql_where=text("is_active AND referred_subscriber_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(referred_party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(referred_party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL "
            "AND length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_referrals_party_binding_evidence",
        ),
        CheckConstraint(
            "(referred_subscriber_id IS NULL AND subscriber_linked_at IS NULL AND "
            "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
            "(referred_subscriber_id IS NOT NULL AND subscriber_linked_at IS NULL "
            "AND subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) "
            "OR (referred_subscriber_id IS NOT NULL AND subscriber_linked_at IS NOT "
            "NULL AND subscriber_link_source IS NOT NULL AND subscriber_link_reason "
            "IS NOT NULL AND length(trim(subscriber_link_source)) > 0 AND "
            "length(trim(subscriber_link_reason)) > 0)",
            name="ck_referrals_subscriber_link_evidence",
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
    referred_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parties.id", ondelete="RESTRICT"),
        index=True,
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    # Legacy compatibility and later reviewed account attachment.
    referred_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), index=True
    )
    subscriber_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    subscriber_link_source: Mapped[str | None] = mapped_column(String(80))
    subscriber_link_reason: Mapped[str | None] = mapped_column(Text)
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
    referred_party = relationship("Party", foreign_keys=[referred_party_id])
    code = relationship(
        "ReferralCode", back_populates="referrals", foreign_keys=[referral_code_id]
    )
    lead = relationship("Lead", foreign_keys=[referred_lead_id])
