"""Move ``subscribers.metadata["splynx_billing_email"]`` onto billing contacts.

Revision ID: 665_backfill_splynx_billing_email_contacts
Revises: 664_purge_retired_splynx_metadata_keys
Create Date: 2026-10-10

## Why

The Splynx import left a separate billing address on about 4,300 subscriber
rows as the undeclared metadata key ``splynx_billing_email``. Its only reader
was the billing-email fallback in
``app/services/web_subscriber_details._build_subscriber_enrichment``.

The typed home for an account's billing address already exists and is what
billing delivery reads: a ``subscriber_contacts`` row flagged
``is_billing_contact`` (``communication_intents._subscriber_addresses`` narrows
billing mail to such contacts), with the account holder's own
``subscribers.email`` always included and used by invoices and receipts. This
revision backfills that home and removes the key from every row whose value
now lives there. The reader is cut over in the same change
(``customer_portal_contacts.account_billing_email``).

## What it does (re-derived at apply time)

Every subscriber row whose metadata is a JSON object holding the key is
locked (``FOR UPDATE``) and classified in Python with the contact owner's
normalisation (``customer_portal_contacts.validated_contact_email`` then
``customer_identity_normalization.normalize_email_identifier``: strip, refuse
``,``/``;``, match the owner's email pattern, lowercase):

* **invalid** -- not a string, blank, more than one address, not an email, or
  longer than the column. Left untouched.
* **already typed** -- equals the account email or the email of one of the
  account's billing contacts. The key is removed; nothing is written.
* **conflict** -- the account already has a billing contact with a different
  non-empty email. Left untouched for human review; never overwritten.
* **backfill** -- otherwise. One ``subscriber_contacts`` row is inserted
  (``contact_type='billing'``, ``is_billing_contact=true``,
  ``receives_notifications=false``, ``is_authorized=false``, a provenance note),
  plus its ``customer_identity_index`` email row exactly as the owner's
  ``rebuild_identity_index_for_subscriber`` would write it. Then the key is
  removed.

``receives_notifications`` stays false on purpose: today nothing delivers to
the Splynx billing address, and turning that on (which would also narrow an
account's billing mail to the designated contact) is a product decision, not a
data move.

Only ``splynx_billing_email`` is removed from a row; every other key keeps its
JSON value (the object is re-serialised through ``jsonb``, as in 664).

One ``audit_events`` row (action ``subscriber.splynx_billing_email_moved``)
records counts -- ``backfilled``, ``removed``, ``conflicts``, ``invalid`` --
and the subscriber ids of conflict and invalid rows for review. It contains no
email address.

## Verify

The revision re-counts after writing and raises (rolling everything back) if
any backfilled or already-typed row still carries the key, or if the remaining
rows differ from ``conflicts + invalid``.

## Idempotent

A second run finds only conflict and invalid rows, writes nothing, and writes
no second audit row (one is written only when something changed or no prior
audit row from this revision exists).

## Downgrade: forward-fix only

``downgrade`` is a no-op. The typed data is a superset of what the key held;
the previous image's reader would merely lose a display fallback that no
template renders. Contacts created here carry the provenance note below if an
operator must find them.

## Budgets and volume

About 4,300 rows in production, read once and updated by primary key.
``lock_timeout = 5s``, ``statement_timeout = 10min`` for this revision only
(restored afterwards). Row locks only. A lock timeout fails the upgrade
cleanly; retry is safe.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "665_backfill_splynx_billing_email_contacts"
down_revision: str | None = "664_purge_retired_splynx_metadata_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY = "splynx_billing_email"

#: ``app/services/validation_api.EMAIL_PATTERN`` -- the pattern the contact
#: owner validates with. Copied because a migration must not import app code;
#: a unit test keeps the two in step.
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MULTI_ADDRESS = re.compile(r"[,;]")
_EMAIL_MAX_LENGTH = 255

CONTACT_NOTE = (
    "Billing email imported from Splynx; moved from subscriber metadata by "
    "migration 665_backfill_splynx_billing_email_contacts."
)

_ACTION = "subscriber.splynx_billing_email_moved"
_ENTITY_TYPE = "subscribers.metadata"
_ACTOR_ID = "migration:665_backfill_splynx_billing_email_contacts"
_LOG_PREFIX = "665_backfill_splynx_billing_email_contacts"
_REASON = (
    "Splynx billing email moved from undeclared subscriber metadata to the "
    "typed billing contact. Conflicting and invalid values were left in "
    "place for human review."
)

_OBJECT_ROWS = (
    "metadata IS NOT NULL AND jsonb_typeof(metadata::jsonb) = 'object' "
    "AND metadata::jsonb ? CAST(:key AS text)"
)


def normalize_billing_email(value: object) -> str | None:
    """The contact owner's normalisation, or ``None`` if it would refuse."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > _EMAIL_MAX_LENGTH or _MULTI_ADDRESS.search(raw):
        return None
    if not EMAIL_PATTERN.match(raw):
        return None
    return raw.lower()


def classify(
    value: object, account_email: str | None, billing_contact_emails: Sequence[str]
) -> tuple[str, str | None]:
    """``(outcome, normalized)``; outcome is invalid/typed/conflict/backfill."""

    normalized = normalize_billing_email(value)
    if normalized is None:
        return "invalid", None
    typed = {
        email.strip().lower()
        for email in billing_contact_emails
        if email and email.strip()
    }
    if normalized == (account_email or "").strip().lower() or normalized in typed:
        return "typed", normalized
    if typed:
        return "conflict", normalized
    return "backfill", normalized


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("subscribers") or not inspector.has_table(
        "subscriber_contacts"
    ):
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
        _move(bind, inspector)
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


def _move(bind: sa.engine.Connection, inspector: sa.engine.Inspector) -> None:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, email, metadata::jsonb -> CAST(:key AS text) AS value "
                f"FROM subscribers WHERE {_OBJECT_ROWS} "
                "ORDER BY id FOR UPDATE"
            ),
            {"key": KEY},
        )
        .mappings()
        .all()
    )
    if not rows:
        print(f"{_LOG_PREFIX}: no subscriber row carries {KEY}; nothing to do.")
        return

    ids = [row["id"] for row in rows]
    billing_emails: dict[uuid.UUID, list[str]] = {}
    for contact in bind.execute(
        sa.text(
            "SELECT subscriber_id, email FROM subscriber_contacts "
            "WHERE is_billing_contact AND subscriber_id = ANY(CAST(:ids AS uuid[])) "
            "FOR UPDATE"
        ),
        {"ids": ids},
    ).mappings():
        billing_emails.setdefault(contact["subscriber_id"], []).append(
            contact["email"] or ""
        )

    removable: list[uuid.UUID] = []
    contacts: list[dict[str, object]] = []
    conflicts: list[str] = []
    invalid: list[str] = []
    for row in rows:
        outcome, normalized = classify(
            row["value"], row["email"], billing_emails.get(row["id"], ())
        )
        if outcome == "invalid":
            invalid.append(str(row["id"]))
        elif outcome == "conflict":
            conflicts.append(str(row["id"]))
        else:
            removable.append(row["id"])
            if outcome == "backfill":
                contacts.append(
                    {
                        "id": uuid.uuid4(),
                        "subscriber_id": row["id"],
                        "email": normalized,
                    }
                )
    backfilled = len(contacts)

    if contacts:
        bind.execute(
            sa.text(
                "INSERT INTO subscriber_contacts ("
                "id, subscriber_id, full_name, email, contact_type, "
                "is_billing_contact, is_authorized, receives_notifications, "
                "notes, created_at, updated_at"
                ") VALUES ("
                ":id, :subscriber_id, NULL, :email, 'billing', "
                "true, false, false, :notes, now(), now()"
                ")"
            ),
            [{**contact, "notes": CONTACT_NOTE} for contact in contacts],
        )
        if inspector.has_table("customer_identity_index"):
            bind.execute(
                sa.text(
                    "INSERT INTO customer_identity_index ("
                    "id, identity_type, normalized_value, subscriber_id, "
                    "subscriber_contact_id, subscriber_channel_id, source_table, "
                    "source_field, created_at, updated_at"
                    ") VALUES ("
                    ":id, 'email', :email, :subscriber_id, :contact_id, NULL, "
                    "'subscriber_contacts', 'email', now(), now()"
                    ")"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "email": contact["email"],
                        "subscriber_id": contact["subscriber_id"],
                        "contact_id": contact["id"],
                    }
                    for contact in contacts
                ],
            )

    removed = 0
    if removable:
        removed = bind.execute(
            sa.text(
                "UPDATE subscribers "
                "SET metadata = (metadata::jsonb - CAST(:key AS text))::json "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"key": KEY, "ids": removable},
        ).rowcount

    _verify(bind, removable, len(conflicts) + len(invalid))

    counts = {
        "backfilled": backfilled,
        "removed": int(removed),
        "conflicts": len(conflicts),
        "invalid": len(invalid),
    }
    changed = backfilled > 0 or removed > 0
    if inspector.has_table("audit_events") and (changed or not _audited(bind)):
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
                "actor_label": "alembic migration 665",
                "action": _ACTION,
                "entity_type": _ENTITY_TYPE,
                "metadata": json.dumps(
                    {
                        "reason": _REASON,
                        "key": KEY,
                        "counts": counts,
                        "conflict_subscriber_ids": sorted(conflicts),
                        "invalid_subscriber_ids": sorted(invalid),
                        "reversible": False,
                    },
                    sort_keys=True,
                ),
            },
        )
    print(f"{_LOG_PREFIX}: {counts}.")


def _verify(
    bind: sa.engine.Connection, removable: list[uuid.UUID], expected_left: int
) -> None:
    still_carrying = bind.execute(
        sa.text(
            "SELECT count(*) FILTER (WHERE id = ANY(CAST(:ids AS uuid[]))) AS moved_left, "
            "count(*) AS total_left "
            f"FROM subscribers WHERE {_OBJECT_ROWS}"
        ),
        {"key": KEY, "ids": removable},
    ).one()
    if still_carrying.moved_left or still_carrying.total_left != expected_left:
        raise RuntimeError(
            f"{_LOG_PREFIX}: verification failed: "
            f"{still_carrying.moved_left} moved row(s) still carry {KEY}; "
            f"{still_carrying.total_left} row(s) carry it, expected "
            f"{expected_left} (conflicts + invalid)."
        )


def _audited(bind: sa.engine.Connection) -> bool:
    return (
        bind.execute(
            sa.text(
                "SELECT 1 FROM audit_events "
                "WHERE actor_id = :actor_id AND action = :action LIMIT 1"
            ),
            {"actor_id": _ACTOR_ID, "action": _ACTION},
        ).first()
        is not None
    )


def downgrade() -> None:
    # Forward-fix only: the billing contacts written here are a superset of
    # what the key held, and the previous image's reader only loses a display
    # fallback that no template renders. Contacts carry CONTACT_NOTE.
    return
