"""Explicit logic-only lifecycle injection for legacy savepoint unit fixtures.

These sessions share a fixture connection and prove NO transaction independence.
File-SQLite denial tests and migrated PostgreSQL tests own that evidence.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.services import password_authentication as pa


@pytest.fixture
def authentication_denial_unit_session(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("TEST_DATABASE_URL") or "db_session" not in request.fixturenames:
        return
    fixture_session = cast(Session, request.getfixturevalue("db_session"))

    @contextmanager
    def logic_session() -> Iterator[Session]:
        with Session(
            bind=fixture_session.get_bind(), join_transaction_mode="create_savepoint"
        ) as db:
            yield db

    monkeypatch.setattr(pa.db_session_adapter, "owner_command_session", logic_session)
