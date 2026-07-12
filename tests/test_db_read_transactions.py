from __future__ import annotations

from types import SimpleNamespace

from app.db import finish_read_transaction


class _FakeSession:
    def __init__(
        self,
        *,
        dialect_name: str = "postgresql",
        in_transaction: bool = True,
        in_nested_transaction: bool = False,
    ) -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self._in_transaction = in_transaction
        self._in_nested_transaction = in_nested_transaction
        self.new = set()
        self.dirty = set()
        self.deleted = set()
        self.expire_on_commit = True
        self.commit_count = 0
        self.rollback_count = 0

    def get_bind(self):
        return self._bind

    def in_transaction(self) -> bool:
        return self._in_transaction

    def in_nested_transaction(self) -> bool:
        return self._in_nested_transaction

    def commit(self) -> None:
        assert self.expire_on_commit is False
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def test_finish_read_transaction_commits_open_postgres_read_transaction() -> None:
    db = _FakeSession()

    finish_read_transaction(db)

    assert db.commit_count == 1
    assert db.rollback_count == 0
    assert db.expire_on_commit is True


def test_finish_read_transaction_ignores_non_postgres_or_nested_transactions() -> None:
    sqlite_db = _FakeSession(dialect_name="sqlite")
    nested_db = _FakeSession(in_nested_transaction=True)
    inactive_db = _FakeSession(in_transaction=False)

    finish_read_transaction(sqlite_db)
    finish_read_transaction(nested_db)
    finish_read_transaction(inactive_db)

    assert sqlite_db.commit_count == 0
    assert nested_db.commit_count == 0
    assert inactive_db.commit_count == 0
    assert sqlite_db.rollback_count == 0
    assert nested_db.rollback_count == 0
    assert inactive_db.rollback_count == 0


def test_finish_read_transaction_leaves_sessions_with_pending_writes_alone() -> None:
    db = _FakeSession()
    db.dirty.add(object())

    finish_read_transaction(db)

    assert db.commit_count == 0
    assert db.rollback_count == 0
