"""Native leads/pipeline, quotes, and sales-orders verticals ported from the
CRM (Phase 3 §1.3–§1.5).

CRM shapes (``dotmac_crm/app/models/crm/sales.py`` and
``dotmac_crm/app/models/sales_order.py``) carried verbatim with the sub
conventions applied — table names drop the ``crm_`` prefix (§1.1):
``crm_pipelines``→``pipelines``, ``crm_pipeline_stages``→``pipeline_stages``,
``crm_leads``→``leads``, ``crm_quotes``→``quotes``,
``crm_quote_line_items``→``quote_line_items``; ``sales_orders`` /
``sales_order_lines`` keep their names.

* PG enums become String columns + app-level enums (§1.7 vocabularies).
* Customer-party FKs re-point at sub ``subscribers.id`` (§1.8, party
  backfill §3.2): ``leads.person_id``/``quotes.person_id``/
  ``sales_orders.person_id`` become ``subscriber_id`` NOT NULL.
* Staff FKs are dropped, UUIDs carried verbatim (§1.8):
  ``quotes.owner_person_id`` (staff map for display) and the Phase 4
  ``owner_agent_id`` columns on leads/sales orders (→ ``crm_agents``).
* ``leads.campaign_id``/``campaign_recipient_id`` are plain UUIDs until the
  Phase 4 campaign tables materialize the FKs (§1.3).
* ``quote_line_items.inventory_item_id`` / ``sales_order_lines
  .inventory_item_id`` are plain UUIDs — inventory is Phase 5 (§1.4).
* The CRM partial unique ``uq_crm_leads_one_open_per_person_pipeline`` is
  recreated on ``(subscriber_id, COALESCE(pipeline_id, zero-uuid))`` by the
  expand-B migration as ``uq_leads_one_open_per_subscriber_pipeline`` — it is
  a Postgres expression index, so it lives in the migration only (the
  app-level ``lead_dedup_enabled`` guard relies on it as its race backstop).
* ``sales_orders.order_number`` continues the CRM ``SO-%06d`` sequence via
  sub's existing ``document_sequences`` (the backfill inserts the CRM row's
  ``next_value``, §1.5).

CRM UUID PKs are kept verbatim by the import (§3.4). The ``quotes`` table
coexists with ``quote_mirror`` until the Phase 3 contract PR (§3.3).
"""

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class LeadStatus(enum.Enum):
    new = "new"
    contacted = "contacted"
    qualified = "qualified"
    proposal = "proposal"
    negotiation = "negotiation"
    won = "won"
    lost = "lost"


class QuoteStatus(enum.Enum):
    draft = "draft"
    sent = "sent"
    accepted = "accepted"
    rejected = "rejected"
    expired = "expired"


class SalesOrderStatus(enum.Enum):
    draft = "draft"
    confirmed = "confirmed"
    paid = "paid"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


class SalesOrderPaymentStatus(enum.Enum):
    pending = "pending"
    partial = "partial"
    paid = "paid"
    waived = "waived"


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    stages = relationship("PipelineStage", back_populates="pipeline")
    leads = relationship("Lead", back_populates="pipeline")


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipelines.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    default_probability: Mapped[int] = mapped_column(Integer, default=50)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pipeline = relationship("Pipeline", back_populates="stages")
    leads = relationship("Lead", back_populates="stage")


class Lead(Base):
    """Sales lead. Lead persons are prospects/customers, so the CRM
    ``person_id`` re-points at a sub subscriber created by the party
    backfill (§1.3)."""

    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_campaign_id", "campaign_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipelines.id")
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_stages.id")
    )
    # CrmAgent UUID carried verbatim — Phase 4 inbox model, no FK (§1.3/§1.8).
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(40), default=LeadStatus.new.value, nullable=False
    )
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    probability: Mapped[int | None] = mapped_column(Integer)
    expected_close_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(String(200))
    # Normalized vocabulary lives in the sales service (LEAD_SOURCE_OPTIONS,
    # gaining "Portal" during the Phase 3 service port, §1.3).
    lead_source: Mapped[str | None] = mapped_column(String(40))
    # Campaign attribution UUIDs carried verbatim; FKs materialize in Phase 4.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    campaign_recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    region: Mapped[str | None] = mapped_column(String(80))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    pipeline = relationship("Pipeline", back_populates="leads")
    stage = relationship("PipelineStage", back_populates="leads")
    quotes = relationship("Quote", back_populates="lead")

    # Transient (non-persisted) flag set by the dedup path in Leads.create when
    # an existing open lead is returned instead of a new one being created, so
    # callers (e.g. the web route) can surface a distinct "existing lead"
    # notice. Ports with the model (§1.3).
    dedup_returned_existing: bool = False

    @hybrid_property
    def contact_id(self):
        return self.subscriber_id

    @contact_id.expression  # type: ignore[no-redef]
    def contact_id(cls):
        return cls.subscriber_id

    @hybrid_property
    def weighted_value(self) -> Decimal | None:
        """Return estimated_value weighted by probability."""
        if self.estimated_value is None or self.probability is None:
            return None
        return self.estimated_value * Decimal(self.probability) / Decimal(100)


class Quote(Base):
    """Sales quote. ``metadata`` carries the whole portal contract (§1.4):
    source, project_type, install{...}, feasibility{}, deposit_percent,
    estimate_provisional, pricing_mode, deposit{...}. The legacy
    ``metadata.subscriber_external_id`` key is provenance only post-import."""

    __tablename__ = "quotes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id")
    )
    # Staff person UUID (quote owner = lead's agent person) — no FK (§1.4).
    owner_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(20), default=QuoteStatus.draft.value, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    # Applied tax rate percent (e.g. 7.5). When set, tax_total is auto-derived
    # from the subtotal on every recalculation; null = manual tax_total.
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    lead = relationship("Lead", back_populates="quotes")
    line_items = relationship("QuoteLineItem", back_populates="quote")
    sales_order = relationship("SalesOrder", back_populates="quote", uselist=False)

    @hybrid_property
    def sales_order_id(self):
        return self.sales_order.id if self.sales_order else None

    @hybrid_property
    def contact_id(self):
        return self.subscriber_id

    @contact_id.expression  # type: ignore[no-redef]
    def contact_id(cls):
        return cls.subscriber_id


class QuoteLineItem(Base):
    """Quote line (CRM ``CrmQuoteLineItem``). ``amount`` stays server-derived;
    ``metadata.sub_offer_id`` is already a sub CatalogOffer id (§1.4)."""

    __tablename__ = "quote_line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quotes.id"), nullable=False
    )
    # CRM inventory-item UUID carried verbatim — inventory is Phase 5, no FK.
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    # Line discount percent (0-100); amount is net of discount.
    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0.00"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    quote = relationship("Quote", back_populates="line_items")


class SalesOrder(Base):
    """Sales order. The CRM person-mediated SO→sub link collapses to the
    first-class ``subscriber_id`` column (§1.5)."""

    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("order_number", name="uq_sales_orders_order_number"),
        UniqueConstraint("quote_id", name="uq_sales_orders_quote_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quotes.id")
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    # CrmAgent UUID carried verbatim — Phase 4, no FK (§1.5/§1.8).
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str | None] = mapped_column(String(80))
    # SO-%06d via document_sequences key "sales_order_number" (§1.5).
    order_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(
        String(20), default=SalesOrderStatus.draft.value, nullable=False
    )
    payment_status: Mapped[str] = mapped_column(
        String(20), default=SalesOrderPaymentStatus.pending.value, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    balance_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    payment_due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deposit_required: Mapped[bool] = mapped_column(Boolean, default=False)
    deposit_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    contract_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", foreign_keys=[subscriber_id])
    quote = relationship("Quote", back_populates="sales_order")
    lines = relationship("SalesOrderLine", back_populates="sales_order")


class SalesOrderLine(Base):
    """Sales-order line. ``metadata`` carries sub_offer_id +
    selfcare_subscription_id/selfcare_subscription_invoice_id — after the
    native rewire these point at sub's own subscription/invoice rows (§1.5)."""

    __tablename__ = "sales_order_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id"), nullable=False
    )
    # CRM inventory-item UUID carried verbatim — inventory is Phase 5, no FK.
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    sales_order = relationship("SalesOrder", back_populates="lines")
