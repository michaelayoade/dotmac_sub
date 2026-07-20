"""Inbound CRM webhook delivery dedup (idempotency).

A redelivery of the same signed CRM webhook must not re-run side effects — most
visibly a duplicate FCM push. Each processed delivery is recorded under a stable
``delivery_id``: the CRM ``X-Webhook-Delivery-Id`` header when present, else a
deterministic ``uuid5`` of the HMAC signature (identical body -> identical
signature -> same id). A replay therefore collides on the primary key and is
skipped.

Isolated table (created by migration ``205_add_crm_webhook_deliveries``); it has
no rows until a webhook is processed, so existing queries are unaffected.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CrmWebhookDelivery(Base):
    __tablename__ = "crm_webhook_deliveries"

    delivery_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # The raw CRM delivery id when the sender provides one (ticket webhooks do);
    # null for selfcare pushes keyed on the signature-derived delivery_id.
    event_id: Mapped[str | None] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    crm_ticket_id: Mapped[str | None] = mapped_column(String(80))
    crm_comment_id: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="processed")
    result: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
