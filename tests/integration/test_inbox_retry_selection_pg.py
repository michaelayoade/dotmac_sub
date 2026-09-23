"""Inbox retry selection acceptance on the real migrated PostgreSQL schema."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from tests.test_inbox_retry_eligible_batching import (
    _assert_exhausted_batch_does_not_starve,
)


def test_postgresql_exhausted_batch_reaches_eligible_work(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Integration conftest also refuses SQLite and unmigrated targets.
    assert db_session.get_bind().dialect.name == "postgresql"
    _assert_exhausted_batch_does_not_starve(db_session, monkeypatch)
