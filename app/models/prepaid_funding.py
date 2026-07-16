"""Durable authority records for reconstructed prepaid funding positions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PrepaidFundingReconstructionBatch(Base):
    """One reviewed, content-addressed reconstruction manifest."""

    __tablename__ = "prepaid_funding_reconstruction_batches"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_batch_currency",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_prepaid_funding_batch_manifest_hash",
        ),
        CheckConstraint(
            "length(manifest_payload_sha256) = 64",
            name="ck_prepaid_funding_batch_payload_hash",
        ),
        CheckConstraint(
            "length(attestation_sha256) = 64",
            name="ck_prepaid_funding_batch_attestation_hash",
        ),
        CheckConstraint(
            "length(attestation_key_fingerprint_sha256) = 64",
            name="ck_prepaid_funding_batch_attestation_key_hash",
        ),
        CheckConstraint(
            "length(blocker_manifest_sha256) = 64",
            name="ck_prepaid_funding_batch_blocker_hash",
        ),
        CheckConstraint(
            "length(candidate_cohort_sha256) = 64",
            name="ck_prepaid_funding_batch_cohort_hash",
        ),
        Index(
            "uq_prepaid_funding_authority_cutover",
            "is_authority_cutover",
            unique=True,
            postgresql_where=text("is_authority_cutover = true"),
            sqlite_where=text("is_authority_cutover = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    manifest_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    manifest_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    attestation_key_fingerprint_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    attestation_signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    blocker_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_cohort_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    position_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    account_count: Mapped[int] = mapped_column(nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(120), nullable=False)
    is_authority_cutover: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    baselines = relationship(
        "PrepaidFundingBaseline",
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class PrepaidFundingBaseline(Base):
    """Approved customer position through one exact reconstruction timestamp."""

    __tablename__ = "prepaid_funding_baselines"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_baseline_currency",
        ),
        UniqueConstraint(
            "batch_id",
            "account_id",
            "currency",
            name="uq_prepaid_funding_baseline_batch_account_currency",
        ),
        Index(
            "uq_prepaid_funding_baseline_active_account_currency",
            "account_id",
            "currency",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "ix_prepaid_funding_baseline_batch_id",
            "batch_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_funding_reconstruction_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    position_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    batch = relationship(
        "PrepaidFundingReconstructionBatch",
        back_populates="baselines",
    )
    account = relationship("Subscriber")
