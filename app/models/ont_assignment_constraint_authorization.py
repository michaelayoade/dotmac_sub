from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OntAssignmentConstraintAuthorizationRequest(Base):
    """Immutable request bound to one exact clean coverage snapshot."""

    __tablename__ = "ont_assignment_constraint_authorization_requests"
    __table_args__ = (
        UniqueConstraint(
            "request_sha256",
            name="uq_ont_assignment_constraint_authorization_request_sha256",
        ),
        CheckConstraint(
            "length(coverage_report_sha256) = 64 "
            "AND length(cutover_report_sha256) = 64 "
            "AND length(request_sha256) = 64",
            name="ck_ont_assignment_constraint_authorization_request_hashes",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_ont_assignment_constraint_authorization_request_expiry",
        ),
        Index(
            "ix_ont_assignment_constraint_authorization_request_target",
            "target_environment",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_environment: Mapped[str] = mapped_column(String(255), nullable=False)
    coverage_report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cutover_report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    review = relationship(
        "OntAssignmentConstraintAuthorizationReview",
        back_populates="request",
        uselist=False,
    )


class OntAssignmentConstraintAuthorizationReview(Base):
    """Independent immutable approve/decline attestation over one request."""

    __tablename__ = "ont_assignment_constraint_authorization_reviews"
    __table_args__ = (
        UniqueConstraint(
            "authorization_request_id",
            name="uq_ont_assignment_constraint_authorization_review_request",
        ),
        UniqueConstraint(
            "attestation_sha256",
            name="uq_ont_assignment_constraint_authorization_review_attestation",
        ),
        CheckConstraint(
            "action IN ('approve', 'decline')",
            name="ck_ont_assignment_constraint_authorization_review_action",
        ),
        CheckConstraint(
            "requested_by <> reviewed_by",
            name="ck_ont_assignment_constraint_authorization_review_separation",
        ),
        CheckConstraint(
            "length(request_sha256) = 64 "
            "AND length(current_coverage_report_sha256) = 64 "
            "AND length(current_cutover_report_sha256) = 64 "
            "AND length(attestation_sha256) = 64",
            name="ck_ont_assignment_constraint_authorization_review_hashes",
        ),
        Index(
            "ix_ont_assignment_constraint_authorization_reviewed",
            "reviewed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    authorization_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "ont_assignment_constraint_authorization_requests.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_coverage_report_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    current_cutover_report_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    request = relationship(
        "OntAssignmentConstraintAuthorizationRequest", back_populates="review"
    )


__all__ = [
    "OntAssignmentConstraintAuthorizationRequest",
    "OntAssignmentConstraintAuthorizationReview",
]
