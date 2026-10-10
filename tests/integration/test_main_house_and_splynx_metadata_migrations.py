"""PostgreSQL evidence for migrations 663 (house designation) and 664 (purge).

Both migrations run against the real migrated schema through the test's own
connection, so every row they touch is rolled back with the test.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import bindparam, text

from app.models.billing import (
    BillingAccount,
    BillingAccountLedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.subscriber import Reseller, ResellerUser, Subscriber

ROOT = Path(__file__).resolve().parents[2]
HOUSE_MIGRATION = ROOT / "alembic/versions/663_main_canonical_house_reseller.py"
PURGE_MIGRATION = ROOT / "alembic/versions/664_purge_retired_splynx_metadata_keys.py"
HOUSE_ACTOR = "migration:663_main_canonical_house_reseller"
PURGE_ACTOR = "migration:664_purge_retired_splynx_metadata_keys"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind(module: ModuleType, db_session, monkeypatch) -> ModuleType:
    context = MigrationContext.configure(db_session.connection())
    monkeypatch.setattr(module, "op", Operations(context))
    return module


def _audit_rows(db_session, actor_id: str) -> list[dict]:
    rows = db_session.execute(
        text(
            "SELECT action, entity_type, entity_id, metadata FROM audit_events "
            "WHERE actor_id = :actor_id ORDER BY occurred_at"
        ),
        {"actor_id": actor_id},
    ).mappings()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 663: house designation
# --------------------------------------------------------------------------


@pytest.fixture
def house_scenario(db_session):
    """An empty June-style House row and an active, non-house Main (SPL-1)."""

    db_session.execute(
        text("UPDATE resellers SET is_house = false WHERE is_house = true")
    )
    db_session.execute(text("UPDATE resellers SET code = NULL WHERE code = 'SPL-1'"))
    house = Reseller(
        name=f"House {uuid4().hex[:6]}", code=None, is_active=True, is_house=True
    )
    main = Reseller(name="Main", code="SPL-1", is_active=True, is_house=False)
    db_session.add_all([house, main])
    db_session.flush()
    login = ResellerUser(
        reseller_id=house.id, email=f"house-{uuid4().hex}@example.com", is_active=True
    )
    account = BillingAccount(reseller_id=house.id, name="House billing")
    db_session.add_all([login, account])
    db_session.flush()
    return house, main, login, account


def _state(db_session, *ids) -> dict:
    db_session.expire_all()
    rows = db_session.execute(
        text(
            "SELECT id, is_house, is_active FROM resellers WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": list(ids)},
    ).all()
    return {row.id: (row.is_house, row.is_active) for row in rows}


def test_upgrade_moves_designation_audits_and_downgrade_restores(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, login, account = house_scenario
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.upgrade()

    assert _state(db_session, house.id, main.id) == {
        house.id: (False, False),
        main.id: (True, True),
    }
    assert db_session.get(ResellerUser, login.id).is_active is False
    assert db_session.get(BillingAccount, account.id).is_active is True
    audits = _audit_rows(db_session, HOUSE_ACTOR)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == "reseller.house_designation_moved"
    assert audit["entity_type"] == "Reseller"
    assert audit["entity_id"] == str(main.id)
    evidence = audit["metadata"]
    assert evidence["house_reseller_id"] == str(house.id)
    assert evidence["house_is_house_before"] is True
    assert evidence["house_is_active_before"] is True
    assert evidence["main_reseller_id"] == str(main.id)
    assert evidence["main_is_house_before"] is False
    assert evidence["deactivated_reseller_user_ids"] == [str(login.id)]

    # Re-running is a no-op: Main is already the house row.
    migration.upgrade()
    assert len(_audit_rows(db_session, HOUSE_ACTOR)) == 1

    migration.downgrade()
    assert _state(db_session, house.id, main.id) == {
        house.id: (True, True),
        main.id: (False, True),
    }
    assert db_session.get(ResellerUser, login.id).is_active is True


def test_upgrade_refuses_while_house_owns_a_subscriber(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, login, _ = house_scenario
    db_session.add(
        Subscriber(
            first_name="Still",
            last_name="House",
            email=f"still-house-{uuid4().hex}@example.com",
            reseller_id=house.id,
        )
    )
    db_session.flush()
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.upgrade()

    assert _state(db_session, house.id, main.id) == {
        house.id: (True, True),
        main.id: (False, True),
    }
    assert db_session.get(ResellerUser, login.id).is_active is True
    assert _audit_rows(db_session, HOUSE_ACTOR) == []


def test_upgrade_refuses_when_house_billing_account_has_a_balance(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, _, account = house_scenario
    account.balance = Decimal("10.00")
    db_session.flush()
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.upgrade()

    assert _state(db_session, house.id, main.id)[main.id] == (False, True)
    assert _audit_rows(db_session, HOUSE_ACTOR) == []


def test_upgrade_refuses_when_house_billing_account_has_ledger_activity(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, _, account = house_scenario
    db_session.add(
        BillingAccountLedgerEntry(
            billing_account_id=account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("5.00"),
            currency="NGN",
            balance_after=Decimal("0.00"),
        )
    )
    db_session.flush()
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.upgrade()

    assert _state(db_session, house.id, main.id)[house.id] == (True, True)
    assert _audit_rows(db_session, HOUSE_ACTOR) == []


def test_upgrade_refuses_without_a_main_reseller(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, _, _ = house_scenario
    main.code = f"NOT-MAIN-{uuid4().hex[:6]}"
    db_session.flush()
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.upgrade()

    assert _state(db_session, house.id, main.id) == {
        house.id: (True, True),
        main.id: (False, True),
    }
    assert _audit_rows(db_session, HOUSE_ACTOR) == []


def test_upgrade_refuses_an_inactive_main(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, _, _ = house_scenario
    main.is_active = False
    db_session.flush()
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.upgrade()

    assert _state(db_session, house.id, main.id)[house.id] == (True, True)
    assert _audit_rows(db_session, HOUSE_ACTOR) == []


def test_downgrade_without_audit_row_is_a_no_op(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, _, _ = house_scenario
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)

    migration.downgrade()

    assert _state(db_session, house.id, main.id) == {
        house.id: (True, True),
        main.id: (False, True),
    }


def test_downgrade_refuses_when_designation_moved_since(
    db_session, house_scenario, monkeypatch
) -> None:
    house, main, _, _ = house_scenario
    migration = _bind(_load(HOUSE_MIGRATION, "m663"), db_session, monkeypatch)
    migration.upgrade()
    # An operator later moved the designation elsewhere.
    db_session.execute(
        text("UPDATE resellers SET is_house = false WHERE id = :id"), {"id": main.id}
    )
    other = Reseller(name="Later house", is_active=True, is_house=True)
    db_session.add(other)
    db_session.flush()

    migration.downgrade()

    assert _state(db_session, house.id, main.id, other.id) == {
        house.id: (False, False),
        main.id: (False, True),
        other.id: (True, True),
    }


# --------------------------------------------------------------------------
# 664: retired Splynx metadata keys
# --------------------------------------------------------------------------


def test_purge_removes_only_retired_keys_and_audits_counts(
    db_session, subscriber, monkeypatch
) -> None:
    migration = _bind(_load(PURGE_MIGRATION, "m664"), db_session, monkeypatch)
    kept = {
        "splynx_date_add": "2019-01-01",
        "splynx_last_update": "2024-01-01",
        "splynx_deleted": "0",
        "splynx_status": "active",
        "splynx_last_online": "2026-01-01 10:00:00",
        "splynx_gps": "6.5,3.3",
        "splynx_location_id": 4,
        "splynx_billing_email": "billing@example.com",
        "nin_verified": True,
    }
    secret = f"cleartext-{uuid4().hex}"
    subscriber.metadata_ = {
        **kept,
        "splynx_login": "legacy-login",
        "splynx_email": "legacy@example.com",
        "splynx_password_cleartext": secret,
        "splynx_customer_labels": ["a", "b"],
    }
    other = Subscriber(
        first_name="Clean",
        last_name="Row",
        email=f"clean-{uuid4().hex}@example.com",
        reseller_id=subscriber.reseller_id,
        metadata_={"splynx_status": "active"},
    )
    db_session.add(other)
    db_session.flush()
    # A non-object value must never be rewritten, even if it contains a
    # retired key's name as a string element.
    array_row = Subscriber(
        first_name="Array",
        last_name="Row",
        email=f"array-{uuid4().hex}@example.com",
        reseller_id=subscriber.reseller_id,
    )
    db_session.add(array_row)
    db_session.flush()
    # Keep the id: after expire_all, touching array_row reloads the ORM row,
    # whose json value (a list) the object-only attribute refuses.
    array_row_id = array_row.id
    db_session.execute(
        text("UPDATE subscribers SET metadata = CAST(:value AS json) WHERE id = :id"),
        {"value": '["splynx_login"]', "id": array_row_id},
    )
    db_session.flush()
    prior_audits = len(_audit_rows(db_session, PURGE_ACTOR))

    migration.upgrade()

    db_session.expire_all()
    assert db_session.get(Subscriber, subscriber.id).metadata_ == kept
    assert db_session.get(Subscriber, other.id).metadata_ == {"splynx_status": "active"}
    # Read the non-object row through SQL: the ORM attribute only accepts objects.
    array_metadata = db_session.execute(
        text("SELECT metadata::jsonb FROM subscribers WHERE id = :id"),
        {"id": array_row_id},
    ).scalar_one()
    assert array_metadata == ["splynx_login"]

    audits = _audit_rows(db_session, PURGE_ACTOR)
    assert len(audits) == prior_audits + 1
    audit = audits[-1]
    assert audit["action"] == "subscriber.metadata_keys_purged"
    evidence = audit["metadata"]
    assert evidence["keys"] == list(migration.RETIRED_KEYS)
    assert evidence["row_counts"]["splynx_login"] >= 1
    assert evidence["row_counts"]["splynx_password_cleartext"] >= 1
    assert evidence["rows_rewritten"] >= 1
    assert evidence["reversible"] is False
    serialized = json.dumps(evidence)
    for value in (secret, "legacy-login", "legacy@example.com"):
        assert value not in serialized

    # Idempotent: nothing left to remove, so no second audit row.
    migration.upgrade()
    assert len(_audit_rows(db_session, PURGE_ACTOR)) == prior_audits + 1

    migration.downgrade()
    db_session.expire_all()
    assert db_session.get(Subscriber, subscriber.id).metadata_ == kept
