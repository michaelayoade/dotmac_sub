"""Faithful mirror of Splynx's ``billing_transactions`` ledger.

Splynx maintains a granular transaction ledger (credit/debit movements) whose
running net per customer IS the ``customer_billing.deposit`` (verified exactly:
deposit = Σ credit − Σ debit over deleted='0'). The migration imported the
financial *documents* (invoices/payments/credit notes) but not this raw ledger,
so the local AR ledger could not reconstruct the deposit (why #247 reads the
deposit net directly).

This table is a 1:1, low-coupling mirror of ``billing_transactions`` — kept
separate from the operational ``ledger_entries`` (which is derived from
invoices+payments and would double-count if these were merged in). It gives
full transaction-level history at parity with Splynx and reconciles to the
deposit per account.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SplynxBillingTransaction(Base):
    __tablename__ = "splynx_billing_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Splynx billing_transactions.id — unique, drives idempotent re-import.
    splynx_transaction_id: Mapped[int] = mapped_column(
        Integer, unique=True, index=True, nullable=False
    )
    splynx_customer_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    # Local subscriber, when the customer was migrated (nullable otherwise).
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )

    # 'credit' (raises balance) | 'debit' (lowers balance). deposit = Σcredit−Σdebit.
    entry_type: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"))

    category_id: Mapped[int | None] = mapped_column(Integer)
    category_name: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)

    transaction_date: Mapped[date | None] = mapped_column(Date, index=True)
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)

    splynx_invoice_id: Mapped[int | None] = mapped_column(Integer)
    splynx_payment_id: Mapped[int | None] = mapped_column(Integer)
    splynx_credit_note_id: Mapped[int | None] = mapped_column(Integer)
    service_id: Mapped[int | None] = mapped_column(Integer)
    service_type: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str | None] = mapped_column(String(40))

    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
