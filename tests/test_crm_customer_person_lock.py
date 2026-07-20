"""Regression tests for the CRM customer create-path serialization (I-1).

Concurrent ``customer.accepted`` webhooks for one CRM person used to each see
"not found" and both insert, producing two subscribers for one person. The fix
takes a transaction-level advisory lock keyed on ``crm_person_id`` before the
find-or-create, so the second webhook blocks until the first commits and then
matches the existing row. These tests pin the lock's SQL and its guards (the
lock is a no-op off PostgreSQL / when no person id is present, so the SQLite
test harness can't exercise real serialization).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.crm_customers import _lock_crm_person


def _fake_db(dialect: str) -> MagicMock:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = dialect
    return db


def test_lock_issues_advisory_xact_lock_on_postgres() -> None:
    db = _fake_db("postgresql")

    _lock_crm_person(db, {"crm_person_id": "abc-123"})

    assert db.execute.call_count == 1
    sql, params = db.execute.call_args.args
    assert "pg_advisory_xact_lock" in str(sql)
    assert params == {"key": "crm_customer:abc-123"}


def test_lock_is_noop_off_postgres() -> None:
    db = _fake_db("sqlite")

    _lock_crm_person(db, {"crm_person_id": "abc-123"})

    db.execute.assert_not_called()


def test_lock_is_noop_without_person_id() -> None:
    db = _fake_db("postgresql")

    # No crm_person_id — the sales-order/quote/fuzzy match paths aren't
    # keyed on a single person, so we don't lock.
    _lock_crm_person(db, {"crm_sales_order_id": "so-1"})
    _lock_crm_person(db, {})

    db.execute.assert_not_called()
    db.get_bind.assert_not_called()


def test_lock_runs_before_find(monkeypatch) -> None:
    """The lock must be taken before the existence check, not after."""
    from app.services import crm_customers

    calls: list[str] = []

    def _spy_lock(db, metadata):  # noqa: ANN001
        calls.append("lock")

    def _spy_find(db, payload, metadata, display_name):  # noqa: ANN001
        calls.append("find")
        return MagicMock(), "crm_person_id"  # pretend an existing match

    monkeypatch.setattr(crm_customers, "_lock_crm_person", _spy_lock)
    monkeypatch.setattr(crm_customers, "_find_existing_customer", _spy_find)
    monkeypatch.setattr(
        crm_customers, "_update_existing_customer", lambda *a, **k: MagicMock()
    )
    monkeypatch.setattr(
        crm_customers, "_customer_response", lambda subscriber: {"id": "x"}
    )

    crm_customers.upsert_customer_from_payload(
        MagicMock(), {"crm_person_id": "p1", "display_name": "Test User"}
    )

    assert calls == ["lock", "find"]
