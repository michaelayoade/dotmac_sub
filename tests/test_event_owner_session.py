from __future__ import annotations

from app.services.events.dispatcher import _isolated_handler_session
from app.services.events.handlers.owner_session import owner_session


def test_handler_session_uses_the_dispatchers_checked_out_connection(db_session):
    parent_connection = db_session.connection()

    with _isolated_handler_session(db_session) as handler_db:
        assert handler_db.get_bind() is parent_connection
        assert handler_db is not db_session


def test_owner_session_preserves_the_dispatcher_bind(db_session):
    parent_bind = db_session.get_bind()

    with owner_session(db_session) as command_db:
        assert command_db.get_bind() is parent_bind
        assert command_db is not db_session
        assert not command_db.in_transaction()
