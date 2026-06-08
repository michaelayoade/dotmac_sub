"""Idempotency keys for wallet-affecting customer operations.

A client sends a stable key with a money-moving request and re-sends the same
key on retry. The (scope, key) unique constraint lets the server detect a replay
and return the original result instead of performing the operation twice.

Isolated table — existing queries are unaffected, and it simply has no rows
until the migration (or create_all) provisions it.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(String(60), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Id of the entity the operation produced (e.g. the SubscriptionAddOn), so a
    # replay can return the original result.
    ref_id: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
