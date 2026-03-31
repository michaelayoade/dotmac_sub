from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm.session import SessionTransaction

logger = logging.getLogger(__name__)

_AFTER_COMMIT_CALLBACKS_KEY = "_after_commit_callbacks"


def supports_after_commit(session: Any) -> bool:
    return isinstance(session, Session)


def run_after_commit(
    session: Session | Any, callback: Callable[[Session], None]
) -> None:
    if not supports_after_commit(session):
        callback(session)
        return
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        callback(session)
        return
    callbacks_by_tx = session.info.setdefault(_AFTER_COMMIT_CALLBACKS_KEY, {})
    callbacks = callbacks_by_tx.setdefault(id(transaction), [])
    callbacks.append(callback)


def _clear_after_commit_callbacks(session: Session) -> None:
    session.info.pop(_AFTER_COMMIT_CALLBACKS_KEY, None)


def _pop_transaction_callbacks(
    session: Session, transaction: SessionTransaction | None
) -> list[Callable[[Session], None]]:
    if transaction is None:
        return []
    callbacks_by_tx = session.info.get(_AFTER_COMMIT_CALLBACKS_KEY, {})
    return list(callbacks_by_tx.pop(id(transaction), []))


def _append_transaction_callbacks(
    session: Session,
    transaction: SessionTransaction | None,
    callbacks: list[Callable[[Session], None]],
) -> None:
    if transaction is None or not callbacks:
        return
    callbacks_by_tx = session.info.setdefault(_AFTER_COMMIT_CALLBACKS_KEY, {})
    callbacks_by_tx.setdefault(id(transaction), []).extend(callbacks)


@event.listens_for(Session, "after_commit")
def _run_after_commit_callbacks(session: Session) -> None:
    current_nested = session.get_nested_transaction()
    if current_nested is not None:
        nested_callbacks = _pop_transaction_callbacks(session, current_nested)
        _append_transaction_callbacks(session, current_nested.parent, nested_callbacks)
        return

    current_root = session.get_transaction()
    callbacks = _pop_transaction_callbacks(session, current_root)
    bind = session.get_bind()
    for callback in callbacks:
        try:
            callback_session = Session(bind=bind, autoflush=False, autocommit=False)
            try:
                callback(callback_session)
            finally:
                callback_session.close()
        except Exception:
            logger.exception("Deferred after-commit callback failed.")


@event.listens_for(Session, "after_transaction_end")
def _cleanup_after_transaction_end(
    session: Session, transaction: SessionTransaction
) -> None:
    callbacks_by_tx = session.info.get(_AFTER_COMMIT_CALLBACKS_KEY)
    if not callbacks_by_tx:
        return
    callbacks_by_tx.pop(id(transaction), None)
    if not callbacks_by_tx:
        _clear_after_commit_callbacks(session)


@event.listens_for(Session, "after_rollback")
def _clear_after_rollback(session: Session) -> None:
    if not session.in_transaction():
        _clear_after_commit_callbacks(session)


@event.listens_for(Session, "after_soft_rollback")
def _clear_after_soft_rollback(
    session: Session, previous_transaction: SessionTransaction
) -> None:
    callbacks_by_tx = session.info.get(_AFTER_COMMIT_CALLBACKS_KEY)
    if callbacks_by_tx:
        callbacks_by_tx.pop(id(previous_transaction), None)
        if not callbacks_by_tx:
            _clear_after_commit_callbacks(session)
    if not session.in_transaction():
        _clear_after_commit_callbacks(session)
