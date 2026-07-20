"""PostgreSQL advisory locks for long-running Celery tasks."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal

logger = logging.getLogger(__name__)


@contextmanager
def postgres_session_advisory_lock(
    lock_key: int, *, timeout_ms: int = 5000
) -> Generator[bool, None, None]:
    """Hold a session-level advisory lock without an open transaction.

    The connection is intentionally pinned for the lock lifetime because
    PostgreSQL session advisory locks belong to one backend connection. The
    transaction is committed immediately after acquisition, so a long-running
    task keeps at most one idle connection, not an idle-in-transaction session.
    """

    if not isinstance(timeout_ms, int) or timeout_ms < 0 or timeout_ms > 300000:
        timeout_ms = 5000

    conn = SessionLocal.kw["bind"].connect()
    db = Session(bind=conn, autoflush=False)
    acquired = False
    is_pg = conn.dialect.name.startswith("postgres")
    try:
        if is_pg:
            db.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}ms'"))
            acquired = bool(
                db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
                ).scalar()
            )
            db.commit()
        else:
            acquired = True
        yield acquired
    except Exception:
        db.rollback()
        raise
    finally:
        unlocked = not is_pg or not acquired
        if is_pg and acquired:
            try:
                unlocked = bool(
                    db.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                    ).scalar()
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("postgres_advisory_unlock_failed")
        db.close()
        if is_pg and acquired and not unlocked:
            conn.invalidate()
        conn.close()
