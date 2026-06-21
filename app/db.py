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
def form_write(db: Session) -> Generator[None, None, None]:
    """Guard a form handler's DB write so a failure can't poison the error path.

    Admin form handlers follow the shape::

        try:
            with form_write(db):
                service.create(...)        # may commit -> may raise
            return RedirectResponse(...)   # success
        except (IntegrityError, ValidationError, ValueError) as exc:
            # session is guaranteed clean here
            return render_form(db, error=...)   # re-queries the DB

    If the write aborts the transaction (an ``IntegrityError`` from a unique/FK
    constraint, a ``DataError`` from a numeric overflow / over-length value),
    the session is left in an aborted state. Any DB query the ``except`` block
    then runs to re-render the form (``get_sidebar_stats``, ``_base_context``,
    ``build_*_context`` …) would itself fail on the poisoned session, turning a
    recoverable 4xx into a 500.

    Wrapping the write in ``with form_write(db):`` rolls the session back before
    the exception reaches the handler's ``except``, so the re-render always runs
    on a clean session. Prefer this over a bare ``db.rollback()`` in each
    ``except`` — it can't be forgotten and documents intent. See findings
    #19/#24/#26/#27.
    """
    try:
        yield
    except Exception:
        db.rollback()
        raise


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
