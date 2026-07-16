"""Durable cutover-readiness evidence for prepaid access enforcement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PrepaidEnforcementReadiness(Base):
    """Reviewed live-owner plan after signed funding materialization.

    This record authorizes only the initial feature cutover. It is not a balance
    source and is never consulted for an individual suspension after activation.
    """

    __tablename__ = "prepaid_enforcement_readiness"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    intended_activation_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    funding_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    candidate_account_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_account_ids_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    funding_decisions_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reconstruction_evidence_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_by: Mapped[str] = mapped_column(String(120), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
