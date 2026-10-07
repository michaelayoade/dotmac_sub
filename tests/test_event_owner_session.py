from __future__ import annotations

from app.services.events.handlers.owner_session import owner_session


def test_owner_session_preserves_the_dispatcher_bind(db_session):
    parent_bind = db_session.get_bind()

    with owner_session(db_session) as command_db:
        assert command_db.get_bind() is parent_bind
        assert command_db is not db_session
        assert not command_db.in_transaction()
