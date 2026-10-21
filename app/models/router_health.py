"""Router health reconciler state.

``RouterHealthSignal`` is compact per-router signal state written only by
``network.router_health`` (PR1 stores ``config_export``). ``NasCoaOutcomeBucket``
is a durable hourly counter of RADIUS CoA outcomes per NAS, written only by
``network.coa_outcome_evidence`` out of band (ADR 0018). Both are observations,
never intended state.

``NasCoaOutcomeBucket.nas_device_id`` deliberately has no foreign key: the
writer uses its own connection while the enforcement caller may hold row locks
(same reasoning as ADR 0017 section 3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: CoA outcome kinds and dispositions accepted by ``nas_coa_outcome_buckets``.
COA_OUTCOME_KINDS: tuple[str, ...] = ("disconnect", "update", "canary")
COA_OUTCOME_DISPOSITIONS: tuple[str, ...] = (
    "disconnected",
    "session_not_found",
    "disabled",
    "temporarily_suppressed",
    "missing_nas_host",
    "missing_dictionary",
    "invalid_dictionary",
    "missing_secret",
    "rejected",
    "timeout",
    "transport_error",
    "unexpected_response",
    "sent",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class RouterHealthSignal(Base):
    """Per-router signal state (ring buffer, status, first abnormal time)."""

    __tablename__ = "router_health_signals"
    __table_args__ = (
        UniqueConstraint(
            "router_id",
            "signal",
            "scope",
            name="uq_router_health_signals_router_signal_scope",
        ),
        Index("ix_router_health_signals_signal_status", "signal", "status"),
        CheckConstraint(
            "status IN ('ok', 'abnormal', 'unknown')",
            name="ck_router_health_signals_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    router_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routers.id", ondelete="CASCADE"),
        nullable=False,
    )
    signal: Mapped[str] = mapped_column(String(40), nullable=False)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    last_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    #: Short enum-like text (a failure class), never raw router output.
    last_text: Mapped[str | None] = mapped_column(String(80), nullable=True)
    samples: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    first_abnormal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_normal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consecutive_abnormal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    boot_marker: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class NasCoaOutcomeBucket(Base):
    """Hourly count of one CoA (kind, disposition) per NAS device."""

    __tablename__ = "nas_coa_outcome_buckets"
    __table_args__ = (
        UniqueConstraint(
            "nas_device_id",
            "kind",
            "disposition",
            "bucket_start",
            name="uq_nas_coa_outcome_bucket",
        ),
        Index("ix_nas_coa_outcome_buckets_bucket_start", "bucket_start"),
        CheckConstraint(
            f"kind IN ({_quoted(COA_OUTCOME_KINDS)})",
            name="ck_nas_coa_outcome_buckets_kind",
        ),
        CheckConstraint(
            f"disposition IN ({_quoted(COA_OUTCOME_DISPOSITIONS)})",
            name="ck_nas_coa_outcome_buckets_disposition",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # No FK: see module docstring.
    nas_device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    disposition: Mapped[str] = mapped_column(String(30), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
