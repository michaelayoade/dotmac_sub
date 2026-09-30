"""Retire the system-admin account's Main-reseller portal membership.

Revision ID: 633_retire_system_admin_main_reseller_membership
Revises: 632_reviewed_payment_allocation_reversal
Create Date: 2026-09-30

Knowledge slug ``main-reseller-customer-mail-copy-leak`` (confirmed
2026-07-21): every eligible customer-facing transactional notification is
silently copied to whichever addresses are active ``ResellerUser`` rows of a
non-house reseller (``app.services.communication_intents._reseller_addresses``,
called from ``submit`` whenever ``reseller.is_house`` is false). The platform's
system-admin mailbox is registered as the sole active ``ResellerUser`` of the
``Main`` reseller (``code = 'SPL-1'``, ``is_house = false``), so it received a
copy of every one of those notifications -- 3,026 in the 24 hours sampled in
July. The record found no evidence that address actually needs reseller-portal
access to Main.

This migration does not hand-pick that historical row by a hardcoded email,
because production data may have moved on since the July sample and blind-firing
a fix against stale assumptions would be worse than doing nothing. Instead it
re-derives the exact shape the record describes, at apply time, against
whatever the database actually holds: an ACTIVE ``reseller_users`` row under
the reseller coded ``SPL-1`` (non-house) whose email also belongs to an active,
registered ``system_users`` row -- i.e. exactly the condition the structural
guard in ``communication_intents._reseller_addresses`` now excludes at read
time (Part B of the same fix). If that condition matches more than one row, or
matches none (data already changed, or a fresh/empty database such as CI's
scratch DB), this migration deliberately does nothing but leaves a note in the
migration output -- it never guesses and never mutates an ambiguous state.

When exactly one row matches, it is deactivated (``is_active = false``), never
deleted: the row itself is Sub's own record of "this login existed under
Main". Before flipping it, the row's prior state (email, reseller_id,
is_active) is written to ``audit_events`` as a durable, queryable audit
trail -- action ``reseller_user.membership_retired``, entity_type
``ResellerUser`` -- so the change is reversible and inspectable without
relying on database backups. ``downgrade`` reverses it by reading that same
audit row back and reactivating the exact ``ResellerUser`` id it names; it does
nothing if no such audit row exists (e.g. upgrade was a no-op).

This migration does NOT touch the broader Main-vs-House reseller identity
reconciliation (the ~14,863 subscribers currently owned by Main) -- that is
tracked separately and explicitly out of scope here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "633_retire_system_admin_main_reseller_membership"
down_revision: str | None = "632_reviewed_payment_allocation_reversal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESELLER_CODE = "SPL-1"
_ACTION = "reseller_user.membership_retired"
_ENTITY_TYPE = "ResellerUser"
_REASON = (
    "Knowledge slug main-reseller-customer-mail-copy-leak (confirmed "
    "2026-07-21): this address is a registered system_users platform "
    "administrator whose reseller_users membership of Main (SPL-1) caused "
    "every eligible customer notification to be copied to it (3,026 in 24h "
    "in the July sample). No evidence this address needs reseller-portal "
    "access to Main. Retired by migration "
    "633_retire_system_admin_main_reseller_membership."
)


def _has_table(bind: sa.engine.Connection, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()

    if not (
        _has_table(bind, "reseller_users")
        and _has_table(bind, "resellers")
        and _has_table(bind, "system_users")
    ):
        return

    candidates = (
        bind.execute(
            sa.text(
                "SELECT ru.id AS reseller_user_id, ru.email AS email, "
                "       ru.reseller_id AS reseller_id, ru.is_active AS is_active "
                "FROM reseller_users ru "
                "JOIN resellers r ON r.id = ru.reseller_id "
                "JOIN system_users su ON lower(su.email) = lower(ru.email) "
                "WHERE r.code = :code "
                "  AND r.is_house = false "
                "  AND ru.is_active = true "
                "  AND ru.email IS NOT NULL "
                "  AND su.is_active = true"
            ),
            {"code": _RESELLER_CODE},
        )
        .mappings()
        .all()
    )

    if len(candidates) != 1:
        # Either the July row has already changed (no match) or the shape is
        # ambiguous (more than one match). Either way this is not the narrow,
        # verified case this migration is authorized to act on -- do nothing
        # rather than guess. `print` surfaces this in migration/deploy logs.
        print(
            "633_retire_system_admin_main_reseller_membership: expected "
            f"exactly one matching reseller_users row under {_RESELLER_CODE}, "
            f"found {len(candidates)}; leaving all rows untouched."
        )
        return

    row = candidates[0]

    if _has_table(bind, "audit_events"):
        bind.execute(
            sa.text(
                "INSERT INTO audit_events ("
                "id, occurred_at, actor_type, actor_id, actor_label, action, "
                "entity_type, entity_id, status_code, is_success, is_active, "
                "metadata, created_at"
                ") VALUES ("
                ":id, now(), 'system', :actor_id, :actor_label, :action, "
                ":entity_type, :entity_id, 200, true, true, "
                "CAST(:metadata AS JSONB), now()"
                ")"
            ),
            {
                "id": str(uuid.uuid4()),
                "actor_id": "migration:633_retire_system_admin_main_reseller_membership",
                "actor_label": "alembic migration 633",
                "action": _ACTION,
                "entity_type": _ENTITY_TYPE,
                "entity_id": str(row["reseller_user_id"]),
                "metadata": json.dumps(
                    {
                        "reason": _REASON,
                        "reseller_code": _RESELLER_CODE,
                        "reseller_id": str(row["reseller_id"]),
                        "email": row["email"],
                        "is_active_before": bool(row["is_active"]),
                        "is_active_after": False,
                    },
                    sort_keys=True,
                ),
            },
        )

    bind.execute(
        sa.text(
            "UPDATE reseller_users SET is_active = false, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": row["reseller_user_id"]},
    )
    print(
        "633_retire_system_admin_main_reseller_membership: deactivated "
        f"reseller_users.id={row['reseller_user_id']} (email={row['email']}) "
        f"under reseller {_RESELLER_CODE}."
    )


def downgrade() -> None:
    bind = op.get_bind()

    if not (_has_table(bind, "audit_events") and _has_table(bind, "reseller_users")):
        return

    audit_row = (
        bind.execute(
            sa.text(
                "SELECT entity_id FROM audit_events "
                "WHERE action = :action AND entity_type = :entity_type "
                "  AND actor_id = :actor_id "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {
                "action": _ACTION,
                "entity_type": _ENTITY_TYPE,
                "actor_id": (
                    "migration:633_retire_system_admin_main_reseller_membership"
                ),
            },
        )
        .mappings()
        .first()
    )
    if audit_row is None:
        # upgrade() was a no-op (no matching row, or ambiguous match); there is
        # nothing to reverse.
        return

    bind.execute(
        sa.text(
            "UPDATE reseller_users SET is_active = true, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": audit_row["entity_id"]},
    )
