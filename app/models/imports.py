"""Durable bulk-import run tracking.

``ImportRun`` + ``ImportRunRow`` replace the settings-log job history with proper,
queryable, scalable DB records: one run per import (with the input, counts and
status) and one row per input line (raw data, ok/error/skipped, error detail).
Drives the dry-run -> apply split and row-level progress for large CSV/XLSX
imports processed in the background.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ImportRunStatus(enum.Enum):
    pending = "pending"  # created, not yet processed
    running = "running"  # being processed
    dry_run_ready = "dry_run_ready"  # validated, awaiting apply
    completed = "completed"
    failed = "failed"


class ImportRowStatus(enum.Enum):
    pending = "pending"
    ok = "ok"  # validated (dry-run) or imported (apply)
    error = "error"
    skipped = "skipped"


class ImportRun(Base):
    __tablename__ = "import_runs"
    __table_args__ = (
        Index("ix_import_runs_status", "status"),
        Index("ix_import_runs_module", "module"),
        Index("ix_import_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    module: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[ImportRunStatus] = mapped_column(
        Enum(ImportRunStatus, name="importrunstatus", create_constraint=False),
        nullable=False,
        default=ImportRunStatus.pending,
    )
    dry_run: Mapped[bool] = mapped_column(default=True, nullable=False)
    data_format: Mapped[str] = mapped_column(String(20), default="csv", nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255))
    csv_delimiter: Mapped[str] = mapped_column(String(4), default=",", nullable=False)
    column_mapping: Mapped[dict | None] = mapped_column(JSON)
    # Input payload. Inline for now; an object-store key can replace this later
    # without touching the run/row contract.
    input_text: Mapped[str | None] = mapped_column(Text)

    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rows = relationship(
        "ImportRunRow",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ImportRunRow.row_number",
    )


class ImportRunRow(Base):
    __tablename__ = "import_run_rows"
    __table_args__ = (
        # One row record per (run, line) — re-processing is idempotent per run.
        UniqueConstraint("run_id", "row_number", name="uq_import_run_rows_run_line"),
        Index("ix_import_run_rows_run", "run_id"),
        Index("ix_import_run_rows_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[ImportRowStatus] = mapped_column(
        Enum(ImportRowStatus, name="importrowstatus", create_constraint=False),
        nullable=False,
        default=ImportRowStatus.pending,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    run = relationship("ImportRun", back_populates="rows")
