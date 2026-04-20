"""Database session boundary for background and infrastructure workflows."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services.adapters import adapter_registry


class DbSessionProvider(Protocol):
    """Application boundary for opening short-lived database sessions."""

    def create_session(self) -> Session: ...

    def session(self) -> Generator[Session, None, None]: ...

    def read_session(self) -> Generator[Session, None, None]: ...

    def advisory_lock(
        self,
        lock_key: int,
        *,
        shared: bool = False,
        timeout_ms: int = 5000,
    ) -> Generator[tuple[Session, bool], None, None]: ...


class SqlAlchemySessionAdapter:
    """SQLAlchemy-backed session provider.

    Use ``session`` for write transactions and ``read_session`` for read-only
    lookups that should explicitly rollback before returning the connection to
    the pool. That rollback is important because SQLAlchemy opens transactions
    for SELECTs when autocommit is disabled.
    """

    name = "db.session.sqlalchemy"

    def create_session(self) -> Session:
        return SessionLocal()

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read_session(self) -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    @contextmanager
    def advisory_lock(
        self,
        lock_key: int,
        *,
        shared: bool = False,
        timeout_ms: int = 5000,
    ) -> Generator[tuple[Session, bool], None, None]:
        """Acquire a PostgreSQL advisory lock with timeout protection.

        Args:
            lock_key: Integer key for the advisory lock.
            shared: If True, acquire a shared lock; otherwise exclusive.
            timeout_ms: Statement timeout in milliseconds (default 5000).
                        Prevents indefinite blocking on lock acquisition.

        Yields:
            Tuple of (session, acquired) where acquired indicates if lock was obtained.
        """
        # Validate timeout to prevent injection (defense in depth)
        if not isinstance(timeout_ms, int) or timeout_ms < 0 or timeout_ms > 300000:
            timeout_ms = 5000

        db = SessionLocal()
        acquired = False
        lock_fn = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
        unlock_fn = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
        try:
            # Set statement timeout to prevent indefinite blocking
            # Note: SET statements require literal values, not parameters
            db.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}ms'"))
            acquired = bool(
                db.execute(text(f"SELECT {lock_fn}(:key)"), {"key": lock_key}).scalar()
            )
            yield db, acquired
            if acquired:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        finally:
            if acquired:
                try:
                    db.execute(text(f"SELECT {unlock_fn}(:key)"), {"key": lock_key})
                    db.commit()
                except Exception:
                    db.rollback()
            db.close()


db_session_adapter = SqlAlchemySessionAdapter()
adapter_registry.register(db_session_adapter)
