"""Sub-owned correlation evidence for payment email delivery coverage.

Tenant foreign keys are installed by migration 637 after the operator-tenant
provider, as for domain_settings/523. Sub's historical squash renders current
public ORM metadata before that provider, so these cannot be inline ORM FKs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PaymentEmailEpisode(Base):
    __tablename__ = "payment_email_episodes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "payment_id",
            "invoice_id",
            "recipient",
            name="uq_payment_email_episode_identity",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_payment_email_episode_scope"),
        UniqueConstraint("notification_id", name="uq_payment_email_episode_delivery"),
        CheckConstraint(
            "deadline_at <= created_at + interval '60 seconds' AND deadline_at >= created_at",
            name="ck_payment_email_collection_bound",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class PaymentEmailPart(Base):
    __tablename__ = "payment_email_parts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            [
                "communication_intent_recipients.tenant_id",
                "communication_intent_recipients.id",
            ],
            ondelete="RESTRICT",
            name="fk_payment_email_part_decision_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "episode_id"],
            ["payment_email_episodes.tenant_id", "payment_email_episodes.id"],
            ondelete="CASCADE",
            name="fk_payment_email_part_episode_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_event_id",
            "recipient",
            name="uq_payment_email_part_source",
        ),
        UniqueConstraint("episode_id", "kind", name="uq_payment_email_part_kind"),
        UniqueConstraint("decision_id", name="uq_payment_email_part_decision"),
        CheckConstraint(
            "kind IN ('receipt', 'invoice_paid')", name="ck_payment_email_part_kind"
        ),
        CheckConstraint("length(trim(body)) > 0", name="ck_payment_email_part_body"),
        CheckConstraint("content_version > 0", name="ck_payment_email_part_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    content_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    content_version: Mapped[int] = mapped_column(nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class PaymentEmailCutover(Base):
    """One reviewed content authority switch for the operator tenant."""

    __tablename__ = "payment_email_cutovers"
    composition_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    receipt_legacy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_legacy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    receipt_content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    invoice_content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    activated_by: Mapped[str] = mapped_column(String(200), nullable=False)
