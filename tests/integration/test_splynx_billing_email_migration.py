"""PostgreSQL evidence for migration 665 (Splynx billing email -> contact).

The migration runs against the real migrated schema through the test's own
connection, so every row it touches is rolled back with the test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.models.customer_identity import CustomerIdentityIndex
from app.models.subscriber import Subscriber, SubscriberContact

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/665_backfill_splynx_billing_email_contacts.py"
ACTOR = "migration:665_backfill_splynx_billing_email_contacts"
KEY = "splynx_billing_email"


def _bind(db_session, monkeypatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("m665", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = MigrationContext.configure(db_session.connection())
    monkeypatch.setattr(module, "op", Operations(context))
    return module


def _audits(db_session) -> list[dict]:
    rows = db_session.execute(
        text(
            "SELECT action, entity_type, metadata FROM audit_events "
            "WHERE actor_id = :actor_id ORDER BY occurred_at"
        ),
        {"actor_id": ACTOR},
    ).mappings()
    return [dict(row) for row in rows]


def _metadata(db_session, subscriber_id) -> dict:
    return db_session.execute(
        text("SELECT metadata::jsonb FROM subscribers WHERE id = :id"),
        {"id": subscriber_id},
    ).scalar_one()


def _contacts(db_session, subscriber_id) -> list[SubscriberContact]:
    return list(
        db_session.scalars(
            select(SubscriberContact)
            .where(SubscriberContact.subscriber_id == subscriber_id)
            .order_by(SubscriberContact.created_at, SubscriberContact.id)
        ).all()
    )


def _subscriber(db_session, reseller_id, tag: str, metadata: dict) -> Subscriber:
    row = Subscriber(
        first_name=tag.title(),
        last_name="Row",
        email=f"{tag}-owner-{uuid4().hex}@example.com",
        reseller_id=reseller_id,
    )
    db_session.add(row)
    db_session.flush()
    # The closed-key owner refuses the undeclared key, so write the legacy
    # shape straight to the column as the Splynx import left it.
    db_session.execute(
        text("UPDATE subscribers SET metadata = CAST(:value AS json) WHERE id = :id"),
        {"value": json.dumps(metadata), "id": row.id},
    )
    db_session.flush()
    return row


def _add_contact(db_session, subscriber, **fields) -> SubscriberContact:
    contact = SubscriberContact(subscriber_id=subscriber.id, **fields)
    db_session.add(contact)
    db_session.flush()
    return contact


def test_backfill_no_overwrite_conflict_invalid_and_idempotent(
    db_session, subscriber, monkeypatch
) -> None:
    reseller_id = subscriber.reseller_id
    token = uuid4().hex
    backfill_email = f"billing-{token}@example.com"
    conflict_value = f"splynx-{token}@example.com"
    finance_email = f"finance-{token}@example.com"
    shared_email = f"shared-{token}@example.com"

    # 1. No billing contact: backfilled through the owner's normalisation.
    backfill = _subscriber(
        db_session,
        reseller_id,
        "backfill",
        {KEY: f"  {backfill_email.upper()}  ", "splynx_status": "active"},
    )
    general = _add_contact(
        db_session,
        backfill,
        email=f"general-{token}@example.com",
        contact_type="general",
        is_billing_contact=False,
    )

    # 2. Equal to the account email: removed, nothing written.
    equal_account = _subscriber(db_session, reseller_id, "equal-account", {})
    db_session.execute(
        text("UPDATE subscribers SET metadata = CAST(:value AS json) WHERE id = :id"),
        {
            "value": json.dumps({KEY: equal_account.email.upper()}),
            "id": equal_account.id,
        },
    )

    # 3. Equal to an existing billing contact: removed, contact untouched.
    equal_contact = _subscriber(
        db_session, reseller_id, "equal-contact", {KEY: shared_email}
    )
    existing = _add_contact(
        db_session,
        equal_contact,
        email=shared_email.upper(),
        contact_type="billing",
        is_billing_contact=True,
    )

    # 4. Conflict with a different non-empty billing contact: untouched.
    conflict = _subscriber(
        db_session,
        reseller_id,
        "conflict",
        {KEY: conflict_value, "splynx_status": "active"},
    )
    finance = _add_contact(
        db_session,
        conflict,
        email=finance_email,
        contact_type="billing",
        is_billing_contact=True,
    )

    # 5. Invalid values: untouched.
    invalid = _subscriber(
        db_session,
        reseller_id,
        "invalid",
        {KEY: f"a-{token}@example.com, b-{token}@example.com"},
    )
    not_string = _subscriber(db_session, reseller_id, "not-string", {KEY: 42})
    db_session.flush()

    migration = _bind(db_session, monkeypatch)
    migration.upgrade()
    db_session.expire_all()

    # 1. backfilled
    assert _metadata(db_session, backfill.id) == {"splynx_status": "active"}
    contacts = _contacts(db_session, backfill.id)
    assert len(contacts) == 2
    created = next(c for c in contacts if c.id != general.id)
    assert created.email == backfill_email
    assert created.contact_type == "billing"
    assert created.is_billing_contact is True
    assert created.receives_notifications is False
    assert created.is_authorized is False
    assert created.person_party_id is None
    assert created.notes == migration.CONTACT_NOTE
    assert db_session.get(SubscriberContact, general.id).email == (
        f"general-{token}@example.com"
    )
    index = db_session.scalars(
        select(CustomerIdentityIndex).where(
            CustomerIdentityIndex.subscriber_contact_id == created.id
        )
    ).all()
    assert [(r.identity_type, r.normalized_value, r.source_table) for r in index] == [
        ("email", backfill_email, "subscriber_contacts")
    ]

    # 2./3. already typed: key removed, no contact written, nothing overwritten
    assert _metadata(db_session, equal_account.id) == {}
    assert _contacts(db_session, equal_account.id) == []
    assert _metadata(db_session, equal_contact.id) == {}
    assert [c.id for c in _contacts(db_session, equal_contact.id)] == [existing.id]
    assert db_session.get(SubscriberContact, existing.id).email == shared_email.upper()

    # 4. conflict: neither side changed
    assert _metadata(db_session, conflict.id) == {
        KEY: conflict_value,
        "splynx_status": "active",
    }
    assert [c.id for c in _contacts(db_session, conflict.id)] == [finance.id]
    assert db_session.get(SubscriberContact, finance.id).email == finance_email

    # 5. invalid: untouched
    assert KEY in _metadata(db_session, invalid.id)
    assert _metadata(db_session, not_string.id) == {KEY: 42}

    audits = _audits(db_session)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == "subscriber.splynx_billing_email_moved"
    assert audit["entity_type"] == "subscribers.metadata"
    evidence = audit["metadata"]
    counts = evidence["counts"]
    assert counts["backfilled"] >= 1
    assert counts["removed"] >= 3
    assert counts["conflicts"] >= 1
    assert counts["invalid"] >= 2
    assert str(conflict.id) in evidence["conflict_subscriber_ids"]
    assert {str(invalid.id), str(not_string.id)} <= set(
        evidence["invalid_subscriber_ids"]
    )
    serialized = json.dumps(evidence).lower()
    assert "@" not in serialized
    for value in (backfill_email, conflict_value, finance_email, shared_email):
        assert value not in serialized

    # Idempotent: only conflict/invalid rows remain, so nothing is written
    # and no second audit row appears.
    contact_ids = {c.id for c in _contacts(db_session, backfill.id)}
    migration.upgrade()
    db_session.expire_all()
    assert {c.id for c in _contacts(db_session, backfill.id)} == contact_ids
    assert _metadata(db_session, conflict.id)[KEY] == conflict_value
    assert len(_audits(db_session)) == 1

    migration.downgrade()
    assert _metadata(db_session, backfill.id) == {"splynx_status": "active"}


def test_no_row_carrying_the_key_is_a_quiet_no_op(
    db_session, subscriber, monkeypatch
) -> None:
    db_session.execute(
        text(
            "UPDATE subscribers "
            "SET metadata = (metadata::jsonb - CAST(:key AS text))::json "
            "WHERE metadata IS NOT NULL "
            "AND jsonb_typeof(metadata::jsonb) = 'object'"
        ),
        {"key": KEY},
    )
    migration = _bind(db_session, monkeypatch)

    migration.upgrade()

    assert _audits(db_session) == []
