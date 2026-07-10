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
    depends_on: tuple[str, ...] = ()  # No dependencies - this is foundational

    def health_check(self) -> tuple[bool, str]:
        """Verify database connectivity with a simple query."""
        db = None
        try:
            db = SessionLocal()
            result = db.execute(text("SELECT 1")).scalar()
            if result == 1:
                return True, "Database connection OK"
            return False, f"Unexpected result from SELECT 1: {result}"
        except Exception as exc:
            return False, f"Database connection failed: {exc}"
        finally:
            if db is not None:
                try:
                    db.rollback()
                    db.close()
                except Exception:
                    pass

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

        # PIN one connection for the whole lock lifetime. Advisory locks are
        # SESSION-level (they belong to one Postgres backend), but a plain
        # Session releases its pooled connection at every commit/rollback the
        # caller performs — so under pool contention the final unlock can land
        # on a DIFFERENT connection, silently return false ("you don't own
        # this lock"), and strand the lock on a connection that goes back to
        # the pool holding it forever (bit the infrastructure poller in prod).
        # Binding the Session to an explicitly checked-out Connection
        # guarantees lock and unlock hit the same backend.
        conn = SessionLocal.kw["bind"].connect()
        db = Session(bind=conn, autoflush=False)
        acquired = False
        unlocked = False
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
                    unlocked = bool(
                        db.execute(
                            text(f"SELECT {unlock_fn}(:key)"), {"key": lock_key}
                        ).scalar()
                    )
                    db.commit()
                except Exception:
                    db.rollback()
            db.close()
            if acquired and not unlocked:
                # Could not prove the unlock happened: kill the raw DBAPI
                # connection rather than return it to the pool still holding
                # the session-level lock.
                conn.invalidate()
            conn.close()


db_session_adapter = SqlAlchemySessionAdapter()
adapter_registry.register(db_session_adapter)
