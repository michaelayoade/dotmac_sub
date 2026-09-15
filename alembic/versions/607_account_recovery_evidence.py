"""Create customer.account_recovery evidence tables and backfill both legacy
deletion-evidence lineages.

Revision ID: 607_account_recovery_evidence
Revises: 606_project_task_subtasks
Create Date: 2026-09-13

Two competing legacy lineages recorded a subscriber deletion before this
owner existed, both as JSON in ``subscribers.metadata_``:

1. ``app/services/account_deletion.py`` (self-service soft-delete) — keys
   ``account_deletion_requested_at`` / ``account_deletion_reason``. This
   lineage NEVER touched anything beyond the subscriber's own subscriptions
   (it only calls `transition_account_status`), so it is safe to backfill as
   affecting exactly ``{subscription}`` — that is a true structural fact
   about that code path, not an invented narrower history.

2. ``app/services/web_system_restore_tool.py``'s retired cascade — keys
   ``recovery_deleted_at`` / ``recovery_deleted_by`` / ``recovery_snapshot``
   / ``recovery_purge_due_at`` / ``recovery_purged_at``. This lineage
   touched invoices, payments, service orders, RADIUS accounts/users, IP/ONT
   /splitter assignments, and CPE devices — resources with NO registered
   recovery participant today. A row from this lineage is backfilled with
   the union of every non-empty resource category actually present in its
   ``recovery_snapshot``, PLUS every non-subscription category this cascade
   was capable of touching whenever the snapshot itself cannot prove a
   narrower scope (fail-closed: never claim "subscription-only" for a tool
   that mutated more than subscriptions unless the stored snapshot proves it
   did not, in this instance).

Legacy JSON keys are removed from ``metadata_`` only after the corresponding
typed row exists, in the same migration, so there is never a window with
neither representation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "607_account_recovery_evidence"
down_revision: str | None = "606_project_task_subtasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DELETION_INTENT_VALUES = (
    "customer_requested_termination",
    "administrative_termination",
    "administrative_recoverable_deletion",
)

# Every resource type the retired web_system_restore_tool.py cascade could
# touch beyond subscriptions. A legacy row from that lineage is backfilled
# with `subscription` plus whichever of these its stored snapshot proves
# were non-empty; snapshot categories this migration cannot introspect
# (invoices/payments/RADIUS/IP/ONT/splitter/CPE were mutated directly, not
# recorded in the JSON snapshot's own limited category list) are always
# included — the snapshot only ever recorded subscriptions, service_orders,
# and cpe_devices, so invoice/payment/radius/ip/ont/splitter involvement can
# never be excluded from a tool-driven deletion and must always be assumed.
_CASCADE_ALWAYS_AFFECTED = (
    "invoice",
    "payment",
    "radius_account",
    "radius_user",
    "ip_assignment",
    "ont_assignment",
    "splitter_assignment",
)


def upgrade() -> None:
    op.create_table(
        "account_recovery_records",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("deletion_intent", sa.String(length=48), nullable=False),
        sa.Column("requested_by", sa.String(length=160), nullable=False),
        sa.Column("deleted_by", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="open"
        ),
        sa.Column(
            "affected_resource_types",
            postgresql.ARRAY(sa.String(length=48)),
            nullable=False,
        ),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column(
            "confirmation_fingerprint", sa.String(length=128), nullable=False
        ),
        sa.Column(
            "fingerprint_revision", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_by", sa.String(length=160), nullable=True),
        sa.Column("rebaselined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rebaselined_by", sa.String(length=160), nullable=True),
        sa.Column("rebaseline_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("command_id", name="uq_account_recovery_command_id"),
        sa.CheckConstraint(
            "length(confirmation_fingerprint) >= 32",
            name="ck_account_recovery_fingerprint_length",
        ),
        sa.CheckConstraint(
            "deletion_intent in ('" + "','".join(_DELETION_INTENT_VALUES) + "')",
            name="ck_account_recovery_deletion_intent",
        ),
        sa.CheckConstraint(
            "(state = 'open' AND restored_at IS NULL) OR "
            "(state = 'blocked' AND restored_at IS NULL) OR "
            "(state = 'restored' AND restored_at IS NOT NULL)",
            name="ck_account_recovery_state_timestamps",
        ),
    )
    op.create_index(
        "uq_account_recovery_one_open_generation",
        "account_recovery_records",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('open', 'blocked')"),
    )

    op.create_table(
        "account_recovery_subscription_snapshots",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "recovery_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account_recovery_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("pre_deletion_status", sa.String(length=32), nullable=False),
        sa.Column(
            "pre_deletion_offer_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.UniqueConstraint(
            "recovery_record_id",
            "subscription_id",
            name="uq_account_recovery_snapshot_subscription",
        ),
    )

    _backfill_legacy_evidence()


def _backfill_legacy_evidence() -> None:
    """Create typed rows for both legacy lineages, then strip their JSON keys.

    Runs as raw SQL/Python inside the migration transaction so it is
    forward-only and safe to re-run (guarded by NOT EXISTS on account_id +
    generation 1 — the first, and in practice only, legacy generation).
    """
    conn = op.get_bind()

    rows = conn.execute(
        sa.text(
            "SELECT id, metadata_ FROM subscribers "
            "WHERE metadata_ IS NOT NULL AND ("
            "  metadata_::jsonb ? 'account_deletion_requested_at' "
            "  OR metadata_::jsonb ? 'recovery_deleted_at'"
            ")"
        )
    ).fetchall()

    for subscriber_id, metadata_raw in rows:
        metadata = metadata_raw if isinstance(metadata_raw, dict) else json.loads(
            metadata_raw or "{}"
        )

        already = conn.execute(
            sa.text(
                "SELECT 1 FROM account_recovery_records "
                "WHERE account_id = :account_id LIMIT 1"
            ),
            {"account_id": subscriber_id},
        ).first()
        if already:
            continue

        subscription_rows = conn.execute(
            sa.text(
                "SELECT id, status, offer_version_id FROM subscriptions "
                "WHERE subscriber_id = :account_id"
            ),
            {"account_id": subscriber_id},
        ).fetchall()

        has_tool_lineage = bool(metadata.get("recovery_deleted_at"))
        has_self_service_lineage = bool(
            metadata.get("account_deletion_requested_at")
        )

        if has_tool_lineage:
            snapshot = metadata.get("recovery_snapshot") or {}
            affected = {"subscription"}
            if isinstance(snapshot, dict):
                if snapshot.get("service_orders"):
                    affected.add("service_order")
                if snapshot.get("cpe_devices"):
                    affected.add("cpe_device")
            # Fail closed: this cascade could touch invoices, payments,
            # RADIUS, IP/ONT/splitter assignments, and the snapshot never
            # recorded those categories at all, so their involvement can
            # never be excluded from the stored evidence alone.
            affected.update(_CASCADE_ALWAYS_AFFECTED)
            deletion_intent = "administrative_recoverable_deletion"
            deleted_by = str(metadata.get("recovery_deleted_by") or "system_restore_tool")
            deleted_at = metadata.get("recovery_deleted_at")
            reason = "Backfilled from retired web_system_restore_tool.py lineage"
            last_restored_at = metadata.get("recovery_last_restored_at")
            if last_restored_at:
                state = "restored"
                restored_at = last_restored_at
                restored_by = str(
                    metadata.get("recovery_last_restored_by") or deleted_by
                )
            else:
                state = "open"
                restored_at = None
                restored_by = None
        elif has_self_service_lineage:
            # Structurally true: this lineage only ever calls
            # `transition_account_status`, which only cancels subscriptions.
            affected = {"subscription"}
            deletion_intent = "customer_requested_termination"
            deleted_by = "customer:self_service_deletion"
            deleted_at = metadata.get("account_deletion_requested_at")
            reason = metadata.get("account_deletion_reason") or (
                "Backfilled from retired account_deletion.py metadata lineage"
            )
            # Self-service deletion was never recoverable, so it is
            # backfilled directly as restored. `deleted_at` is the only
            # timestamp evidence this lineage recorded, so it also stands in
            # for `restored_at` to satisfy the state/timestamp CHECK.
            state = "restored"
            restored_at = deleted_at
            restored_by = deleted_by
        else:
            continue

        record_id = conn.execute(sa.text("SELECT gen_random_uuid()")).scalar()
        command_id = conn.execute(sa.text("SELECT gen_random_uuid()")).scalar()
        correlation_id = conn.execute(sa.text("SELECT gen_random_uuid()")).scalar()
        fingerprint_source = "|".join(
            [
                str(subscriber_id),
                "1",
                deletion_intent,
                ",".join(sorted(affected)),
                "1",
            ]
        )
        # Computed in Python with hashlib, not Postgres's pgcrypto `digest()`
        # — no migration in this chain installs that extension, and this
        # mirrors the exact algorithm `account_recovery.py::_fingerprint`
        # uses at runtime (see other migrations, e.g. 268/474, which take
        # the same approach for the identical reason).
        fingerprint = hashlib.sha256(
            fingerprint_source.encode("utf-8")
        ).hexdigest()

        conn.execute(
            sa.text(
                "INSERT INTO account_recovery_records ("
                "id, account_id, generation, deletion_intent, requested_by, "
                "deleted_by, reason, requested_at, deleted_at, state, "
                "affected_resource_types, command_id, correlation_id, "
                "confirmation_fingerprint, fingerprint_revision, "
                "restored_at, restored_by"
                ") VALUES ("
                ":id, :account_id, 1, :deletion_intent, :requested_by, "
                ":deleted_by, :reason, :deleted_at, :deleted_at, :state, "
                ":affected, :command_id, :correlation_id, :fingerprint, 1, "
                ":restored_at, :restored_by"
                ")"
            ),
            {
                "id": record_id,
                "account_id": subscriber_id,
                "deletion_intent": deletion_intent,
                "requested_by": deleted_by,
                "deleted_by": deleted_by,
                "reason": reason,
                "deleted_at": deleted_at,
                "state": state,
                "affected": sorted(affected),
                "command_id": command_id,
                "correlation_id": correlation_id,
                "fingerprint": fingerprint,
                "restored_at": restored_at,
                "restored_by": restored_by,
            },
        )

        for sub_id, status, offer_version_id in subscription_rows:
            conn.execute(
                sa.text(
                    "INSERT INTO account_recovery_subscription_snapshots ("
                    "id, recovery_record_id, subscription_id, "
                    "pre_deletion_status, pre_deletion_offer_version_id"
                    ") VALUES ("
                    "gen_random_uuid(), :record_id, :subscription_id, "
                    ":status, :offer_version_id"
                    ")"
                ),
                {
                    "record_id": record_id,
                    "subscription_id": sub_id,
                    "status": status,
                    "offer_version_id": offer_version_id,
                },
            )

        cleaned = dict(metadata)
        for key in (
            "account_deletion_requested_at",
            "account_deletion_reason",
            "recovery_deleted_at",
            "recovery_deleted_by",
            "recovery_purge_due_at",
            "recovery_purged_at",
            "recovery_snapshot",
            "recovery_last_restored_at",
            "recovery_last_restored_by",
        ):
            cleaned.pop(key, None)
        conn.execute(
            sa.text("UPDATE subscribers SET metadata_ = :metadata WHERE id = :id"),
            {"metadata": json.dumps(cleaned), "id": subscriber_id},
        )


def downgrade() -> None:
    # Tombstones must not silently disappear. Downgrading this migration is
    # refused once any typed row exists post-cutover (including the legacy
    # backfill this same migration performs) rather than attempting a lossy
    # rehydration back into the two retired, incompatible JSON shapes.
    conn = op.get_bind()
    count = conn.execute(
        sa.text("SELECT count(*) FROM account_recovery_records")
    ).scalar()
    if count:
        raise RuntimeError(
            "Refusing to downgrade 607_account_recovery_evidence: "
            f"{count} account_recovery_records row(s) exist and downgrading "
            "would silently drop tombstone lineage. Manually verify and "
            "clear before downgrading."
        )
    op.drop_table("account_recovery_subscription_snapshots")
    op.drop_index(
        "uq_account_recovery_one_open_generation",
        table_name="account_recovery_records",
    )
    op.drop_table("account_recovery_records")
