"""Pre-change backup of a subscriber's connectivity state.

The connectivity reconciler (and the legacy enforcement paths it will absorb)
mutate destructive, hard-to-reverse state: external ``radcheck``/``radreply``
rows are deleted, ``AccessCredential``/``RadiusUser.is_active`` flags flip, and
``subscription.ipv4_address`` is nulled on cancel — historically with **no
trail and no way back** (the R2 "the column is the only copy of the address"
incident class). This table captures that state *before* a mutation so a bad
convergence is auditable and restorable.

It is a backup record, not a source of truth: the reconciler never reads it to
make decisions. Capture is best-effort and must never break the mutation it
guards. Rows are pruned by a retention task.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.db import Base


class ConnectivityStateBackup(Base):
    """One point-in-time snapshot of a subscriber's connectivity state."""

    __tablename__ = "connectivity_state_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Why the snapshot was taken: cancel|suspend|restore|converge|manual.
    reason: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )
    captured_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # External RADIUS rows as captured (list of {username, attribute, op, value}).
    radcheck: Mapped[list | None] = mapped_column(JSON, nullable=True)
    radreply: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Internal credential/radius-user flags
    # (list of {credential_id, login, credential_active, radius_user_active}).
    credentials: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # IP state: {subscriptions: [{id, ipv4_address, ipv6_address}],
    #            assignments: [{id, ip_version, address, is_active, allocation_type}]}.
    ip_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Restore audit — set when this backup is applied back.
    restored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    restored_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
