from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

if TYPE_CHECKING:
    from app.services.unit_of_work import UnitOfWork


class Base(DeclarativeBase):
    pass


def get_engine():
    connect_args = {}
    if settings.database_url.startswith(("postgresql://", "postgresql+")):
        server_options = (
            f"-c statement_timeout={settings.db_statement_timeout_ms} "
            f"-c lock_timeout={settings.db_lock_timeout_ms} "
            "-c idle_in_transaction_session_timeout="
            f"{settings.db_idle_in_transaction_session_timeout_ms}"
        )
        connect_args["options"] = server_options
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        connect_args=connect_args,
    )


_engine = get_engine()


SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


def dispose_engine() -> None:
    """Dispose pooled DB connections, especially after Celery prefork."""
    _engine.dispose()


def get_db():
    """Centralized database session dependency for FastAPI.

    Yields a database session and ensures it is closed after the request.
    Use this as a dependency in FastAPI route handlers.

    Example:
        @app.get("/items")
        def get_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def task_session() -> Generator[Session, None, None]:
    """Context manager for database sessions in Celery tasks.

    Creates a new session and ensures proper cleanup. Commits on success,
    rolls back on exception.

    Example:
        with task_session() as db:
            db.query(Model).all()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_uow() -> Generator["UnitOfWork", None, None]:
    """FastAPI dependency for unit-of-work pattern with auto-commit.

    Provides a UnitOfWork that automatically commits on successful request
    completion and rolls back on exception. Use this for routes that need
    explicit transaction control.

    Unlike get_db(), this dependency:
    - Auto-commits on success (no need to call db.commit() in services)
    - Services should use flush() instead of commit()
    - Provides clear transaction boundary semantics

    Example:
        from app.db import get_uow
        from app.services.unit_of_work import UnitOfWork

        @router.post("/items")
        def create_item(
            data: ItemCreate,
            uow: UnitOfWork = Depends(get_uow),
        ):
            with uow:
                item = item_service.create(uow.session, data)
                return item
    """
    from app.services.unit_of_work import UnitOfWork

    session = SessionLocal()
    try:
        uow = UnitOfWork(session, auto_commit=True)
        yield uow
    finally:
        session.close()
