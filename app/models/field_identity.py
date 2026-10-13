"""Restricted, manually reviewed field identity revisions; never customer avatars."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IdentityState(StrEnum):
    pending = "pending"
    reviewed = "reviewed"
    rejected = "rejected"
    revoked = "revoked"


class FieldIdentityRevision(Base):
    __tablename__ = "field_identity_revisions"
    __table_args__ = (
        UniqueConstraint("subject_id", "revision", name="uq_field_identity_revision"),
        CheckConstraint(
            "state IN ('pending','reviewed','rejected','revoked')",
            name="ck_field_identity_state",
        ),
        CheckConstraint(
            "revision > 0 AND version > 0", name="ck_field_identity_versions"
        ),
        CheckConstraint(
            "(state = 'pending' AND reviewer_id IS NULL) OR (state <> 'pending' AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND reviewer_id <> subject_id AND length(trim(review_reason)) > 0)",
            name="ck_field_identity_review",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IdentityState.pending.value
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    company_name: Mapped[str] = mapped_column(String(120), nullable=False)
    portrait_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=False
    )
    evidence_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class FieldIdentityReceipt(Base):
    __tablename__ = "field_identity_receipts"
    __table_args__ = (
        UniqueConstraint(
            "subject_id", "idempotency_key", name="uq_field_identity_receipt"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
