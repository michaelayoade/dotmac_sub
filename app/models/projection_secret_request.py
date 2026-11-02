"""Open single-use authorizations for projection secret fetch (ADR-0076 §7).

A row is opened by the credential owner when it authors a projection plan
and burned by the one fetch that redeems it. The row never holds secret
material: it names the credential, the secret version the plan was
authored against, and an expiry. Fetch refuses when the credential's
current version differs (the secret rotated under the plan), when the
request is expired or already redeemed, or when the credential is gone or
inactive — every refusal is the same opaque outcome to the caller.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProjectionSecretRequest(Base):
    __tablename__ = "projection_secret_requests"
    __table_args__ = (
        UniqueConstraint("request_ref", name="uq_projection_secret_requests_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_ref: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_credentials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
