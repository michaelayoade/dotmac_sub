"""Durable evidence for canonical prepaid coverage reconciliation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PrepaidCoverageReconciliationRun(Base):
    """One immutable, fingerprint-bound coverage repair decision."""

    __tablename__ = "prepaid_coverage_reconciliation_runs"
    __table_args__ = (
        Index(
            "uq_prepaid_coverage_reconciliation_runs_idempotency",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_prepaid_coverage_reconciliation_runs_created_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_subscription_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entitlement_created_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    already_covered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    no_repair_required_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    quarantined_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    items = relationship(
        "PrepaidCoverageReconciliationItem",
        back_populates="run",
        order_by="PrepaidCoverageReconciliationItem.subscription_id",
    )


class PrepaidCoverageReconciliationItem(Base):
    """Immutable exact evidence or quarantine result for one subscription."""

    __tablename__ = "prepaid_coverage_reconciliation_items"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "subscription_id",
            name="uq_prepaid_coverage_reconciliation_item_subscription",
        ),
        CheckConstraint(
            "decision IN ('entitlement_created', 'already_covered', "
            "'no_repair_required', 'quarantined')",
            name="ck_prepaid_coverage_reconciliation_item_decision",
        ),
        CheckConstraint(
            "source_type IN ('service_entitlement', 'service_extension', "
            "'invoice_line', 'account_adjustment', 'none')",
            name="ck_prepaid_coverage_reconciliation_item_source_type",
        ),
        CheckConstraint(
            "(source_type = 'service_entitlement' AND "
            "source_entitlement_id IS NOT NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NULL) OR "
            "(source_type = 'service_extension' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NOT NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NULL) OR "
            "(source_type = 'invoice_line' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NOT NULL AND "
            "source_account_adjustment_id IS NULL) OR "
            "(source_type = 'account_adjustment' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NOT NULL) OR "
            "(source_type = 'none' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NULL)",
            name="ck_prepaid_coverage_reconciliation_item_exact_source",
        ),
        CheckConstraint(
            "ends_at IS NULL OR starts_at IS NOT NULL",
            name="ck_prepaid_coverage_reconciliation_item_period_pair",
        ),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_prepaid_coverage_reconciliation_item_period_order",
        ),
        Index(
            "ix_prepaid_coverage_reconciliation_items_subscription",
            "subscription_id",
            "created_at",
        ),
        Index(
            "ix_prepaid_coverage_reconciliation_items_reason",
            "decision",
            "reason_code",
        ),
        Index(
            "ix_prepaid_coverage_reconciliation_items_entitlement",
            "entitlement_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_coverage_reconciliation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_entitlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_entitlements.id", ondelete="RESTRICT"),
    )
    source_service_extension_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_extension_entries.id", ondelete="RESTRICT"),
    )
    source_invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_lines.id", ondelete="RESTRICT"),
    )
    source_account_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_adjustments.id", ondelete="RESTRICT"),
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    entitlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_entitlements.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    run = relationship("PrepaidCoverageReconciliationRun", back_populates="items")
    subscription = relationship("Subscription")
    account = relationship("Subscriber")
    entitlement = relationship(
        "ServiceEntitlement",
        foreign_keys=[entitlement_id],
    )
    source_entitlement = relationship(
        "ServiceEntitlement",
        foreign_keys=[source_entitlement_id],
    )
    source_service_extension_entry = relationship("ServiceExtensionEntry")
    source_invoice_line = relationship("InvoiceLine")
    source_account_adjustment = relationship("AccountAdjustment")
