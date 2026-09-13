"""Persistence for ``customer.account_recovery`` — deletion/recovery evidence.

One row per deletion "generation" on an account: immutable identity, the
typed reason it was deleted, tombstone/restoration/re-baseline state, the
exact set of resource types the deletion affected, and the current
confirmation fingerprint used to gate restoration. A child table records the
exact subscription references touched by that generation and their
pre-deletion state — never a JSON snapshot.

Tombstones are never erased: a restored or re-baselined generation keeps its
row forever as lineage evidence (see ``AccountRecoveryState``).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    column,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# The closed, typed reason a deletion happened. Only
# `administrative_recoverable_deletion` is restorable through this owner;
# the other two values exist so a legacy row backfilled from either retired
# lineage (`account_deletion.py`'s self-service flag, or
# `web_system_restore_tool.py`'s prior snapshot/cascade tool) can still be
# represented honestly even though today only recoverable deletions are
# created going forward by the reviewed admin recovery tool.
CUSTOMER_REQUESTED_TERMINATION = "customer_requested_termination"
ADMINISTRATIVE_TERMINATION = "administrative_termination"
ADMINISTRATIVE_RECOVERABLE_DELETION = "administrative_recoverable_deletion"

DELETION_INTENT_VALUES = (
    CUSTOMER_REQUESTED_TERMINATION,
    ADMINISTRATIVE_TERMINATION,
    ADMINISTRATIVE_RECOVERABLE_DELETION,
)

# The closed vocabulary of resource types a deletion generation may name as
# affected. Only `subscription` is a REGISTERED recovery participant today
# (see `app/services/account_recovery.py::REGISTERED_RECOVERY_PARTICIPANTS`);
# the remaining values exist so legacy rows backfilled from the retired
# `web_system_restore_tool.py` cascade can name what it actually touched
# without inventing a narrower, false "subscription-only" history.
RESOURCE_TYPE_SUBSCRIPTION = "subscription"
RESOURCE_TYPE_INVOICE = "invoice"
RESOURCE_TYPE_PAYMENT = "payment"
RESOURCE_TYPE_SERVICE_ORDER = "service_order"
RESOURCE_TYPE_RADIUS_ACCOUNT = "radius_account"
RESOURCE_TYPE_RADIUS_USER = "radius_user"
RESOURCE_TYPE_IP_ASSIGNMENT = "ip_assignment"
RESOURCE_TYPE_ONT_ASSIGNMENT = "ont_assignment"
RESOURCE_TYPE_SPLITTER_ASSIGNMENT = "splitter_assignment"
RESOURCE_TYPE_CPE_DEVICE = "cpe_device"

KNOWN_RESOURCE_TYPES = frozenset(
    {
        RESOURCE_TYPE_SUBSCRIPTION,
        RESOURCE_TYPE_INVOICE,
        RESOURCE_TYPE_PAYMENT,
        RESOURCE_TYPE_SERVICE_ORDER,
        RESOURCE_TYPE_RADIUS_ACCOUNT,
        RESOURCE_TYPE_RADIUS_USER,
        RESOURCE_TYPE_IP_ASSIGNMENT,
        RESOURCE_TYPE_ONT_ASSIGNMENT,
        RESOURCE_TYPE_SPLITTER_ASSIGNMENT,
        RESOURCE_TYPE_CPE_DEVICE,
    }
)


class AccountRecoveryState(str, enum.Enum):
    """Legal lifecycle states for one deletion generation.

    ``open`` -> ``blocked`` (attempted, found an unsupported participant,
    no mutation happened) is legal and repeatable. ``open``/``blocked`` ->
    ``restored`` is the only mutating transition this owner performs
    directly. Re-baselining (see `rebaseline_recovery_evidence`) is a
    reviewed correction of a generation's own evidence — it never moves the
    record out of ``open``/``blocked``, so a re-baselined generation stays
    restorable; it is recorded via ``rebaselined_at``/``rebaselined_by``/
    ``rebaseline_reason`` instead of a separate terminal state. A generation
    that is re-baselined but never restored stays ``open`` or ``blocked``
    forever, exactly like one that was never re-baselined.
    """

    open = "open"
    blocked = "blocked"
    restored = "restored"


class AccountRecoveryRecord(Base):
    """One deletion generation: tombstone, evidence, and restoration state."""

    __tablename__ = "account_recovery_records"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_account_recovery_command_id"),
        CheckConstraint(
            "length(confirmation_fingerprint) >= 32",
            name="ck_account_recovery_fingerprint_length",
        ),
        CheckConstraint(
            "deletion_intent in ('"
            + "','".join(DELETION_INTENT_VALUES)
            + "')",
            name="ck_account_recovery_deletion_intent",
        ),
        # Exactly one open (or blocked-but-not-yet-restored) generation per
        # account at a time. Postgres treats NULL as distinct so this partial
        # unique index only ever fires on the un-terminated state.
        Index(
            "uq_account_recovery_one_open_generation",
            "account_id",
            unique=True,
            postgresql_where=(
                (column("state") == "open") | (column("state") == "blocked")
            ),
        ),
        CheckConstraint(
            "(state = 'open' AND restored_at IS NULL) OR "
            "(state = 'blocked' AND restored_at IS NULL) OR "
            "(state = 'restored' AND restored_at IS NOT NULL)",
            name="ck_account_recovery_state_timestamps",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)

    deletion_intent: Mapped[str] = mapped_column(String(48), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    deleted_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    state: Mapped[AccountRecoveryState] = mapped_column(
        Enum(AccountRecoveryState, native_enum=False, length=16),
        nullable=False,
        default=AccountRecoveryState.open,
    )

    affected_resource_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(48)), nullable=False
    )

    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))

    confirmation_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_by: Mapped[str | None] = mapped_column(String(160))

    rebaselined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebaselined_by: Mapped[str | None] = mapped_column(String(160))
    rebaseline_reason: Mapped[str | None] = mapped_column(Text)

    subscription_snapshots = relationship(
        "AccountRecoverySubscriptionSnapshot",
        back_populates="recovery_record",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AccountRecoverySubscriptionSnapshot(Base):
    """One subscription's exact pre-deletion reference for one generation."""

    __tablename__ = "account_recovery_subscription_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "recovery_record_id",
            "subscription_id",
            name="uq_account_recovery_snapshot_subscription",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recovery_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_recovery_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pre_deletion_status: Mapped[str] = mapped_column(String(32), nullable=False)
    pre_deletion_offer_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )

    recovery_record = relationship(
        "AccountRecoveryRecord", back_populates="subscription_snapshots"
    )
