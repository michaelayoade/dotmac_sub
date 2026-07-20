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
    Boolean,
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
        Index("uq_import_runs_source_run_id", "source_run_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_runs.id", ondelete="RESTRICT"),
        nullable=True,
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
    source_run = relationship(
        "ImportRun",
        remote_side=[id],
        foreign_keys=[source_run_id],
        back_populates="applied_run",
    )
    applied_run = relationship(
        "ImportRun",
        foreign_keys=[source_run_id],
        back_populates="source_run",
        uselist=False,
    )
    created_payments = relationship(
        "Payment",
        back_populates="import_run",
        foreign_keys="Payment.import_run_id",
    )
    payment_batch_reversal = relationship(
        "PaymentImportBatchReversal",
        back_populates="import_run",
        uselist=False,
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
    # Payment imports need durable provenance before a later rollback may move
    # money. NULL means historical/unverified; False means the row idempotently
    # reused a payment created elsewhere; True means this run created it.
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        index=True,
    )
    record_created: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    run = relationship("ImportRun", back_populates="rows")
    payment = relationship("Payment", foreign_keys=[payment_id])
    payment_batch_reversal_item = relationship(
        "PaymentImportBatchReversalItem",
        back_populates="import_run_row",
        uselist=False,
    )


class PaymentImportBatchReversal(Base):
    """Confirmed reversal of payments provably created by one import run."""

    __tablename__ = "payment_import_batch_reversals"
    __table_args__ = (
        Index(
            "uq_payment_import_batch_reversals_run_id",
            "import_run_id",
            unique=True,
        ),
        Index(
            "uq_payment_import_batch_reversals_idempotency_key",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    preview_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    reversed_payment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    skipped_reused_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    import_run = relationship("ImportRun", back_populates="payment_batch_reversal")
    items = relationship(
        "PaymentImportBatchReversalItem",
        back_populates="batch_reversal",
        order_by="PaymentImportBatchReversalItem.created_at",
    )


class PaymentImportBatchReversalItem(Base):
    """Exact source settlement and resulting reversal evidence for one row."""

    __tablename__ = "payment_import_batch_reversal_items"
    __table_args__ = (
        UniqueConstraint(
            "batch_reversal_id",
            "payment_id",
            name="uq_payment_import_batch_reversal_item_payment",
        ),
        Index(
            "uq_payment_import_batch_reversal_items_run_row_id",
            "import_run_row_id",
            unique=True,
        ),
        Index(
            "uq_payment_import_batch_reversal_items_reversal_id",
            "payment_reversal_id",
            unique=True,
        ),
        Index(
            "uq_payment_import_batch_reversal_items_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_import_batch_reversal_items_consumption_entry_id",
            "credit_consumption_ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_reversal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_import_batch_reversals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    import_run_row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_run_rows.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_settlement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_settlements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_reversal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_reversals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    credit_consumption_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    source_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    batch_reversal = relationship("PaymentImportBatchReversal", back_populates="items")
    import_run_row = relationship(
        "ImportRunRow", back_populates="payment_batch_reversal_item"
    )
    payment = relationship("Payment", foreign_keys=[payment_id])
    payment_settlement = relationship("PaymentSettlement")
    payment_reversal = relationship("PaymentReversal")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    credit_consumption_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[credit_consumption_ledger_entry_id]
    )
