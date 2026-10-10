"""Purge unread, undeclared Splynx import keys from ``subscribers.metadata``.

Revision ID: 664_purge_retired_splynx_metadata_keys
Revises: 663_main_canonical_house_reseller
Create Date: 2026-10-10

## Why

``subscribers.metadata`` has a closed key space
(``app/services/subscriber_metadata_keys.DECLARED_METADATA_KEYS``). Only four
Splynx keys are declared, as frozen provenance: ``splynx_date_add``,
``splynx_last_update``, ``splynx_deleted`` and ``splynx_status``.
``app/services/web_subscriber_details`` additionally still reads
``splynx_last_online``, ``splynx_gps``, ``splynx_location_id`` and
``splynx_billing_email``.

The Splynx import left many more keys on production rows. The ones listed in
``RETIRED_KEYS`` below are neither declared nor read by any code in this
repository (verified by search across ``app``, ``scripts``, ``templates`` and
``static`` when this revision was written; the architecture guard
``tests/architecture/test_subscriber_metadata_ownership.py`` keeps it that way).
They include customer contact data (``splynx_email``) and, on any environment
not yet purged operationally, ``splynx_password_cleartext``. Keeping
unowned personal data and a cleartext credential in a blob nothing reads is
pure liability, so this is the contract step for those keys: removal.

``splynx_billing_email`` is deliberately NOT retired here even though it is
undeclared: ``web_subscriber_details._build_subscriber_enrichment`` reads it as
the billing-email fallback on the subscriber detail page. Removing a key a
reader still consumes would silently change what operators see.

## What it does

For every row whose metadata is a JSON object holding at least one retired key,
``metadata = (metadata::jsonb - array[...])::json``. Only those keys are
removed. No value is copied anywhere -- not to ``audit_events``, not to a
backup table, not to logs: the purge is the point.

The column is ``json`` (not ``jsonb``), so a touched row's remaining object is
re-serialised through ``jsonb``: key order and insignificant whitespace may
change, duplicate keys collapse to the last value. The JSON value of every
remaining key is unchanged. Untouched rows are not rewritten.

One ``audit_events`` row (action ``subscriber.metadata_keys_purged``) records
the key list, the per-key row counts measured at apply time, and the number of
rows rewritten. It contains no key values.

## Irreversible

``downgrade`` is a deliberate no-op. The removed values are not retained
anywhere, by design, so there is nothing to restore; re-introducing a cleartext
password or unowned contact data would defeat the change. Rollback of the
application image is unaffected: no code reads these keys.

## Budgets and volume

About 15,200 rows touched in production (the widest keys appear on 15,174
rows). One sequential scan to count, one UPDATE over the matching rows; row
locks only, no table lock beyond ``ROW EXCLUSIVE``. ``lock_timeout = 5s``,
``statement_timeout = 10min`` for this revision only (restored afterwards,
because Alembic runs the whole upgrade in one transaction). A lock timeout fails
the upgrade cleanly; retry is safe because the UPDATE is idempotent (a second
run matches no rows and writes no audit row).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "664_purge_retired_splynx_metadata_keys"
down_revision: str | None = "663_main_canonical_house_reseller"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Undeclared, unread Splynx import keys. Mirrored by
#: ``tests/architecture/test_subscriber_metadata_ownership.py`` so none can be
#: read, written or declared again.
RETIRED_KEYS: tuple[str, ...] = (
    "splynx_login",
    "splynx_category",
    "splynx_partner_percent",
    "splynx_billing_type",
    "splynx_added_by",
    "splynx_added_by_id",
    "splynx_customer_labels",
    "splynx_daily_prepaid_cost",
    "splynx_gdpr_agreed",
    "splynx_email",
    "splynx_email_conflict",
    "splynx_conversion_date",
    "splynx_password_cleartext",
)

_ACTION = "subscriber.metadata_keys_purged"
_ENTITY_TYPE = "subscribers.metadata"
_ACTOR_ID = "migration:664_purge_retired_splynx_metadata_keys"
_LOG_PREFIX = "664_purge_retired_splynx_metadata_keys"
_REASON = (
    "Undeclared Splynx import keys that no code reads. Removed without copying "
    "their values; splynx_password_cleartext included so any environment still "
    "carrying it is cleaned. Irreversible by design."
)

_OBJECT_ROWS = (
    "metadata IS NOT NULL AND jsonb_typeof(metadata::jsonb) = 'object' "
    "AND metadata::jsonb ?| CAST(:keys AS text[])"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("subscribers"):
        return
    if "metadata" not in {
        column["name"] for column in inspector.get_columns("subscribers")
    }:
        return

    previous = bind.execute(
        sa.text(
            "SELECT current_setting('lock_timeout') AS lock_timeout, "
            "current_setting('statement_timeout') AS statement_timeout"
        )
    ).one()
    bind.execute(sa.text("SELECT set_config('lock_timeout', '5s', true)"))
    bind.execute(sa.text("SELECT set_config('statement_timeout', '10min', true)"))
    try:
        _purge(bind, inspector)
    finally:
        bind.execute(
            sa.text(
                "SELECT set_config('lock_timeout', :lock_timeout, true), "
                "set_config('statement_timeout', :statement_timeout, true)"
            ),
            {
                "lock_timeout": previous.lock_timeout,
                "statement_timeout": previous.statement_timeout,
            },
        )


def _purge(bind: sa.engine.Connection, inspector: sa.engine.Inspector) -> None:
    keys = list(RETIRED_KEYS)
    per_key = ", ".join(
        f"count(*) FILTER (WHERE metadata::jsonb ? :key_{index}) AS key_{index}"
        for index in range(len(keys))
    )
    counts_row = (
        bind.execute(
            sa.text(
                f"SELECT {per_key} FROM subscribers WHERE {_OBJECT_ROWS}"  # noqa: S608
            ),
            {"keys": keys, **{f"key_{index}": key for index, key in enumerate(keys)}},
        )
        .mappings()
        .one()
    )
    row_counts = {
        key: int(counts_row[f"key_{index}"]) for index, key in enumerate(keys)
    }

    if not any(row_counts.values()):
        print(f"{_LOG_PREFIX}: no subscriber row carries a retired key; nothing to do.")
        return

    rewritten = bind.execute(
        sa.text(
            "UPDATE subscribers "
            "SET metadata = (metadata::jsonb - CAST(:keys AS text[]))::json "
            f"WHERE {_OBJECT_ROWS}"
        ),
        {"keys": keys},
    ).rowcount

    if inspector.has_table("audit_events"):
        bind.execute(
            sa.text(
                "INSERT INTO audit_events ("
                "id, occurred_at, actor_type, actor_id, actor_label, action, "
                "entity_type, entity_id, status_code, is_success, is_active, "
                "metadata, created_at"
                ") VALUES ("
                ":id, now(), 'system', :actor_id, :actor_label, :action, "
                ":entity_type, NULL, 200, true, true, "
                "CAST(:metadata AS JSONB), now()"
                ")"
            ),
            {
                "id": str(uuid.uuid4()),
                "actor_id": _ACTOR_ID,
                "actor_label": "alembic migration 664",
                "action": _ACTION,
                "entity_type": _ENTITY_TYPE,
                "metadata": json.dumps(
                    {
                        "reason": _REASON,
                        "keys": keys,
                        "row_counts": row_counts,
                        "rows_rewritten": int(rewritten),
                        "reversible": False,
                    },
                    sort_keys=True,
                ),
            },
        )
    print(
        f"{_LOG_PREFIX}: removed {len(keys)} retired key(s) from "
        f"{rewritten} subscriber row(s); per-key counts {row_counts}."
    )


def downgrade() -> None:
    # Irreversible by design: the removed values (including any cleartext
    # password) were not copied anywhere, so there is nothing to restore, and
    # recreating them would defeat the purge. No code reads these keys, so the
    # previous application image runs unchanged without them.
    return
