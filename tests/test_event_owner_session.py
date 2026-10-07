from __future__ import annotations

from sqlalchemy.engine import Connection

from app.services.events.handlers.owner_session import owner_session


def test_owner_session_uses_an_independent_engine_bind(db_session):
    parent_bind = db_session.get_bind()
    assert isinstance(parent_bind, Connection)

    with owner_session(db_session) as command_db:
        assert command_db.get_bind() is parent_bind.engine
        assert command_db.get_bind() is not parent_bind
        assert not command_db.in_transaction()
