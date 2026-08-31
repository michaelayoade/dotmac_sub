"""The server half of one field-mobile OIDC ceremony.

A ceremony row is a SHORT-LIVED, SINGLE-USE binding created when the device
asks Sub to start a federated login, and burned when the device comes back
with the identity provider's assertion. It is not a session, not a credential
and not an identity — it creates no party and grants nothing.

What it deliberately does NOT hold:

* **The nonce.** Only ``nonce_hash`` (SHA-256 hex) is stored. The raw nonce
  travels to the device, into the authorization request, and back inside the
  ID token; a database copy would let anyone with read access mint an
  assertion that satisfies the replay check.
* **The PKCE verifier.** The device generates it, keeps it, and exchanges it
  directly with the identity provider. Sub never receives it and there is no
  column that could accidentally receive it — the shape is the guarantee, not
  a convention. Sub never sees the authorization code either.
* **Anything the identity provider said.** Roles, groups and authorization
  scopes have no effect in Sub (they are not read, not stored, and not
  projected), so there is nowhere for them to leak into an access decision.

``binding_key``, ``issuer``, ``client_id``, ``redirect_uri`` and
``deployment_id`` are PINNED AT START from trusted deployment configuration,
never from a request body or a token claim, and re-compared for exact equality
at exchange. Pinning them is what makes the exchange's binding check mean
something: a configuration change mid-ceremony refuses the outstanding
ceremony instead of silently accepting an assertion issued against the old
configuration.

Single use is a column, not a convention: ``consumed_at`` moves once, under a
row lock, inside the exchange's owner command. ``outcome`` and
``failure_reason`` are SAFE CATEGORIES for operators and metrics — never a
token, a code, a nonce, a subject or any other identity material.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OidcCeremonyOutcome(enum.Enum):
    """How a ceremony ended. ``pending`` is the only non-terminal member."""

    pending = "pending"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class OidcMobileCeremony(Base):
    """One outstanding or burned field-mobile OIDC ceremony."""

    __tablename__ = "oidc_mobile_ceremonies"
    __table_args__ = (
        # One nonce, one ceremony, ever. A repeated nonce hash would let a
        # captured assertion satisfy a second ceremony's replay check.
        UniqueConstraint("nonce_hash", name="uq_oidc_mobile_ceremonies_nonce_hash"),
        CheckConstraint(
            "length(trim(nonce_hash)) = 64",
            name="ck_oidc_mobile_ceremonies_nonce_hash_shape",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_oidc_mobile_ceremonies_expiry_after_start",
        ),
        # A terminal outcome and a consumption timestamp say the same thing and
        # must not disagree: a "completed" ceremony that is still consumable is
        # exactly the row a replay would reuse.
        CheckConstraint(
            "(outcome = 'pending' AND consumed_at IS NULL) "
            "OR (outcome <> 'pending' AND consumed_at IS NOT NULL)",
            name="ck_oidc_mobile_ceremonies_outcome_alignment",
        ),
        Index("ix_oidc_mobile_ceremonies_expiry_sweep", "outcome", "expires_at"),
        Index("ix_oidc_mobile_ceremonies_device", "device_id", "created_at"),
    )

    #: The opaque ceremony id handed to the device. A v4 UUID and nothing else:
    #: it names a row and carries no claim of its own, so possessing one proves
    #: only that this deployment started a ceremony.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: The installed verifier this ceremony belongs to
    #: (``authentication_bindings.binding_key``, mechanism ``oidc``). Stored as
    #: the key rather than the row id so a re-installed binding cannot silently
    #: adopt an outstanding ceremony.
    binding_key: Mapped[str] = mapped_column(String(80), nullable=False)

    #: Configuration pinned at start. Compared for EXACT equality at exchange.
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(120), nullable=False)

    #: SHA-256 hex of the server-issued nonce. The raw value is never stored.
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Free-text only in type: the device's ``X-Device-Id``, already truncated
    #: by the same helper the session table uses.
    device_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Stored as text rather than a native enum: adding a terminal category is
    #: a declaration by this module, never an ``ALTER TYPE`` migration
    #: (ADR-0008). Values come from :class:`OidcCeremonyOutcome`.
    outcome: Mapped[str] = mapped_column(
        String(20), default=OidcCeremonyOutcome.pending.value, nullable=False
    )

    #: A SAFE CATEGORY from the exchange's refusal vocabulary — never a token,
    #: a code, a nonce, a subject, or any other identity material.
    failure_reason: Mapped[str | None] = mapped_column(String(64))
