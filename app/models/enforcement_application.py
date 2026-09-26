"""Enforcement application evidence: what actually happened on a device.

``EnforcementApplication`` is a durable, per-(subscription, NAS device, effect)
observation of the outcome of an enforcement attempt (address-list block,
address-list unblock, or session kick). It is a fact, not a decision: no
resolver or projection may read it as the intended access state.

``access.enforcement_evidence`` (``app/services/enforcement_evidence.py``) is the sole
writer (ADR-0017). No foreign keys are declared on ``subscription_id`` or
``nas_device_id`` deliberately: the writer opens an out-of-band session while
the calling transaction may hold ``SELECT ... FOR UPDATE`` on the subscription
row, and an FK check from a second connection would self-deadlock against
that lock (ADR-0017 §3).
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: Bound on the sanitized detail text column. Generous enough for a full
#: sanitized exception message, small enough to keep a runaway message from
#: bloating the row.
ENFORCEMENT_APPLICATION_DETAIL_MAX_LENGTH = 2000


class EnforcementEffect(enum.StrEnum):
    """What the enforcement attempt was trying to do on the NAS."""

    address_list_block = "address_list_block"
    address_list_unblock = "address_list_unblock"
    session_kick = "session_kick"


class EnforcementOutcomeValue(enum.StrEnum):
    """What actually happened."""

    applied = "applied"
    failed = "failed"
    not_applicable = "not_applicable"


class EnforcementFailureClass(enum.StrEnum):
    """Why a real failure happened, classified once in ``app/services/nas``."""

    auth_rejected = "auth_rejected"
    unreachable = "unreachable"
    timeout = "timeout"
    command_failed = "command_failed"
    not_capable = "not_capable"


class EnforcementPath(enum.StrEnum):
    """Which transport carried the (successful or failed) attempt."""

    ssh = "ssh"
    api = "api"


class EnforcementApplication(Base):
    """Current-state evidence of one (subscription, NAS device, effect).

    One row per ``(subscription_id, nas_device_id, effect)``. A subsequent
    attempt upserts in place: ``attempt_count`` increments on failure and
    resets on a successful application, and ``last_success_at`` records the
    most recent applied outcome.
    """

    __tablename__ = "enforcement_applications"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "nas_device_id",
            "effect",
            name="uq_enforcement_applications_subscription_nas_effect",
        ),
        Index(
            "ix_enforcement_applications_outcome_failure_class",
            "outcome",
            "failure_class",
        ),
        Index(
            "ix_enforcement_applications_nas_device",
            "nas_device_id",
        ),
        CheckConstraint(
            "effect IN ('address_list_block', 'address_list_unblock', 'session_kick')",
            name="ck_enforcement_applications_effect",
        ),
        CheckConstraint(
            "outcome IN ('applied', 'failed', 'not_applicable')",
            name="ck_enforcement_applications_outcome",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN "
            "('auth_rejected', 'unreachable', 'timeout', 'command_failed', "
            "'not_capable')",
            name="ck_enforcement_applications_failure_class",
        ),
        CheckConstraint(
            "path IS NULL OR path IN ('ssh', 'api')",
            name="ck_enforcement_applications_path",
        ),
        # not_applicable is never a failure, and a real failure is never
        # not_applicable (ADR-0017 invariants).
        CheckConstraint(
            "outcome != 'not_applicable' OR failure_class IS NULL",
            name="ck_enforcement_applications_not_applicable_no_failure_class",
        ),
        CheckConstraint(
            "outcome != 'failed' OR failure_class IS NOT NULL",
            name="ck_enforcement_applications_failed_has_failure_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # No FK: see module docstring / ADR-0017 §3.
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    nas_device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    effect: Mapped[str] = mapped_column(String(30), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    path: Mapped[str | None] = mapped_column(String(10), nullable=True)
    detail: Mapped[str | None] = mapped_column(
        String(ENFORCEMENT_APPLICATION_DETAIL_MAX_LENGTH), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
