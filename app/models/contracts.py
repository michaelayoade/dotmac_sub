"""Contract signature models for click-to-sign workflow."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db import Base


class ContractSignature(Base):
    """Record of a signed contract agreement.

    Captures:
    - What was agreed to (agreement_text)
    - Who signed it (signer_name, signer_email)
    - When it was signed (signed_at)
    - Digital fingerprint (ip_address, user_agent)
    - Context (account_id, service_order_id, document_id)
    """
    __tablename__ = "contract_signatures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Links to related entities
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    # Backwards-compatible alias: most of the service layer uses "account_id".
    account_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    service_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_orders.id")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_documents.id")
    )

    # Signer information
    signer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    signer_email: Mapped[str] = mapped_column(String(255), nullable=False)

    # Signature timestamp
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC)
    )

    # Digital fingerprint
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)  # IPv6 max length
    user_agent: Mapped[str | None] = mapped_column(String(500))

    # Copy of what was agreed to (for audit trail)
    agreement_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )

    # Relationships
    subscriber = relationship("Subscriber")
    service_order = relationship("ServiceOrder")
    document = relationship("LegalDocument")
