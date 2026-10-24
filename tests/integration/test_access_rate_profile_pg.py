"""The rate-owner preflight cannot write: PostgreSQL refuses it in the database.

Hosted PostgreSQL only (``tests/integration`` requires a migrated PostgreSQL
engine). SQLite has no read-only transaction, which is why this is not a unit
test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import SessionLocal, begin_read_only_snapshot
from app.services import access_rate_profile as owner


def test_preflight_runs_in_a_read_only_snapshot(engine) -> None:
    assert engine is not None  # migrated PostgreSQL, same DATABASE_URL as SessionLocal
    with SessionLocal() as session:
        begin_read_only_snapshot(session)
        owner.preflight(session)
        with pytest.raises(DBAPIError, match="read-only"):
            session.execute(text("UPDATE radius_profiles SET name = name"))
        session.rollback()


def test_the_cli_hands_the_preflight_a_read_only_session(engine, monkeypatch) -> None:
    assert engine is not None
    attempted: list[str] = []

    def probe(session: Session) -> list:
        with pytest.raises(DBAPIError, match="read-only"):
            session.execute(text("UPDATE radius_profiles SET name = name"))
        attempted.append("refused")
        return []

    monkeypatch.setattr(owner, "preflight", probe)

    assert owner.main(["preflight"]) == 0
    assert attempted == ["refused"]
