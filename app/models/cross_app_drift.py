"""Cross-app drift detection: runs, findings, events, waivers.

A read-only daily control that proves CRM / sub / ERP still agree on the
business facts that matter. Checks emit *findings*; the runner persists them by
a stable fingerprint so drift can be tracked as new / recurring / worsened /
resolved / waived across runs. The detector never heals — it points at the
reconciler that should.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Severity — ordered by how loudly it should page.
SEVERITY_CRITICAL = "critical"  # money, compliance, live unauthorized service
SEVERITY_HIGH = "high"  # duplicate identity, missing ERP customer, tax mismatch
SEVERITY_MEDIUM = "medium"  # stale mirror, orphaned asset, missing link
SEVERITY_LOW = "low"  # metadata disagreement
SEVERITY_ORDER = {
    SEVERITY_LOW: 0,
    SEVERITY_MEDIUM: 1,
    SEVERITY_HIGH: 2,
    SEVERITY_CRITICAL: 3,
}

# Finding lifecycle.
STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_WAIVED = "waived"

# Finding-event kinds.
EVENT_CREATED = "created"
EVENT_RECURRING = "recurring"
EVENT_WORSENED = "worsened"
EVENT_RESOLVED = "resolved"
EVENT_REOPENED = "reopened"
EVENT_WAIVED = "waived"

RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(UTC)


class CrossAppDriftRun(Base):
    """One execution of the detector across all registered checks."""

    __tablename__ = "cross_app_drift_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=RUN_RUNNING, nullable=False)
    checks_run: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    findings_open: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    findings_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    findings_resolved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class CrossAppDriftFinding(Base):
    """A single disagreement, deduped across runs by ``fingerprint``.

    fingerprint = sha256(check_name | entity_type | canonical_entity_id |
    mismatch_type), so the same drift is one row whose ``occurrences`` and
    ``last_seen_at`` advance each run until it resolves.
    """

    __tablename__ = "cross_app_drift_findings"
    __table_args__ = (
        Index("ix_cross_app_drift_findings_status_sev", "status", "severity"),
        Index("ix_cross_app_drift_findings_check", "check_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    check_name: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    canonical_entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    mismatch_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=STATUS_OPEN, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Compact both-sides-of-the-mismatch snapshot (e.g. billing_status vs
    # radius_authorized + active_sessions) — for dashboard / incident review.
    evidence: Mapped[dict | None] = mapped_column(JSON)
    # Remediation metadata: suggested_owner / suggested_action for the human.
    details: Mapped[dict | None] = mapped_column(JSON)


class CrossAppDriftFindingEvent(Base):
    """Append-only history of what happened to a finding on each run."""

    __tablename__ = "cross_app_drift_finding_events"
    __table_args__ = (Index("ix_cross_app_drift_events_finding", "finding_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cross_app_drift_findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    snapshot: Mapped[dict | None] = mapped_column(JSON)


class CrossAppDriftWaiver(Base):
    """An operator's decision to suppress a known finding from alerting.

    The finding is still tracked; it just doesn't page. An expired waiver stops
    suppressing (the finding reopens on the next run that still sees it).
    """

    __tablename__ = "cross_app_drift_waivers"
    __table_args__ = (
        Index("ix_cross_app_drift_waivers_fp_active", "fingerprint", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    waived_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
