"""Retire the temporary CRM live-chat authority control plane.

Revision ID: 569_retire_crm_chat_authority
Revises: 568_paystack_recovery_evidence
Create Date: 2026-08-30

ADR 0006 gave portal live chat to an external CRM behind one selector,
``comms.chat_session_authority``, for the length of a bounded migration. The
CRM was decommissioned on 2026-08-29 and the selector is removed from code in
this change, so two control-plane rows are left with no reader.

Both are also the ONLY surviving evidence of whether that cutover was ever
actually performed in production -- the CRM was deleted without a final backup,
the native models carry no provenance column, and the write barrier wrote
nothing durable when it fired. So this migration is deliberately asymmetric: it
disarms both, and destroys neither.

1. ``domain_settings`` row ``comms``/``chat_session_authority``. Its spec is
   retired, so nothing reads it. Left in place it would be an invisible,
   operator-editable value pinning a decision no code makes -- and the exact
   row a future reader could rediscover and honour. It is DELETED, following
   ``309_retire_feature_aliases``: Sub removes a retired control's row rather
   than orphaning it.

   But the value is recorded FIRST, into ``domain_setting_history`` as an
   explicit ``delete`` transition. That table is written by ORM mapper events
   (``app/models/domain_setting_history.py``), which a raw-SQL migration does
   not trigger, so the row is inserted here by hand. Its ``tenant_id``,
   ``domain`` and ``key`` are denormalised precisely so history outlives the
   setting, and its actor columns are nullable precisely because a migration
   has no actor. Without this step the deployed value would be gone with no
   trace, and the question "was the CRM cutover ever executed" would become
   permanently unanswerable.

2. ``integration_capability_bindings`` rows for ``crm.chat_session.v1``. The
   capability no longer exists in the current ``dotmac.crm`` manifest and Sub
   has no caller for it, so an enabled binding is an armed door to a dead host.
   It is DISABLED, not deleted. ``enabled_at`` / ``disabled_at`` /
   ``created_by`` on this row are the closest thing production holds to a
   direct record that the cutover's preconditions were satisfied -- and,
   because the chat capability ran on the INTERACTIVE path, it produced no
   ``integration_deliveries`` and no ``integration_inbox`` receipts. There is
   no other receipt. Deleting the row would destroy the only one.

Deliberately NOT touched: every ``inbox_conversations`` / ``inbox_messages``
row, every ``integration_inbox`` receipt, every ``integration_config_revisions``
row (a revision carrying ``chat_widget_config_id`` dates the operator's
preparation), the ``dotmac.crm`` installation itself, and every pre-existing
``domain_setting_history`` row. Those are business or audit records. This
migration retires a control; it does not rewrite a history.

Both steps are idempotent. Re-running inserts no duplicate history row, because
the settings row it reads from is gone after the first run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "569_retire_crm_chat_authority"
down_revision: str | None = "568_paystack_recovery_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOMAIN = "comms"
_KEY = "chat_session_authority"
_RETIRED_CAPABILITY = "crm.chat_session.v1"
_REASON = (
    "ADR 0006 retired 2026-08-30: the CRM was decommissioned and Sub's native "
    "Team Inbox is the sole live-chat authority. Spec removed; migration "
    "569_retire_crm_chat_authority deleted the row and recorded its value here."
)


def _has_table(bind: sa.engine.Connection, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "domain_settings"):
        rows = bind.execute(
            sa.text(
                "SELECT id, tenant_id, value_text, value_json, is_secret "
                "FROM domain_settings WHERE domain = :domain AND key = :key"
            ),
            {"domain": _DOMAIN, "key": _KEY},
        )
        rows = rows.mappings().all()

        if rows and _has_table(bind, "domain_setting_history"):
            for row in rows:
                # Mirrors `_stored_text`: a JSON value is recorded as its dumped
                # form, a scalar as its text. A secret's value is never
                # recorded -- this key is not one, but the rule is the table's,
                # not the caller's, so it is honoured here too.
                if row["value_json"] is not None:
                    before = json.dumps(row["value_json"], sort_keys=True)
                else:
                    before = row["value_text"]
                bind.execute(
                    sa.text(
                        "INSERT INTO domain_setting_history ("
                        "id, tenant_id, domain, key, setting_id, action, "
                        "value_before, value_after, secret_changed, changed_at, "
                        "changed_by_party_id, change_reason"
                        ") VALUES ("
                        ":id, :tenant_id, :domain, :key, NULL, 'delete', "
                        ":value_before, NULL, :secret_changed, now(), "
                        "NULL, :reason"
                        ")"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tenant_id": row["tenant_id"],
                        "domain": _DOMAIN,
                        "key": _KEY,
                        "value_before": None if row["is_secret"] else before,
                        "secret_changed": bool(row["is_secret"]),
                        "reason": _REASON,
                    },
                )

        bind.execute(
            sa.text(
                "DELETE FROM domain_settings WHERE domain = :domain AND key = :key"
            ),
            {"domain": _DOMAIN, "key": _KEY},
        )

    if _has_table(bind, "integration_capability_bindings"):
        bind.execute(
            sa.text(
                "UPDATE integration_capability_bindings "
                "SET state = 'disabled', "
                "    disabled_at = COALESCE(disabled_at, now()), "
                "    updated_at = now() "
                "WHERE capability_id = :capability AND state <> 'disabled'"
            ),
            {"capability": _RETIRED_CAPABILITY},
        )


def downgrade() -> None:
    """Deliberately empty.

    Re-enabling the binding would re-arm a capability the current manifest does
    not declare, and recreating the settings row would recreate a decision no
    reader honours. The forward path is a reviewed architecture decision, not a
    downgrade. The deleted value is not lost -- ``domain_setting_history`` holds
    it, which is why the delete was recorded rather than performed silently.
    """
