"""Unit of Work pattern for transaction boundary management.

Provides a consistent contract for transaction handling:
- Auto-commit on successful exit
- Auto-rollback on exception
- Savepoint support for nested transactions

Usage:
    from app.services.unit_of_work import UnitOfWork

    # In FastAPI routes (via get_uow dependency):
    @router.post("/items")
    def create_item(uow: UnitOfWork = Depends(get_uow)):
        with uow:
            item = service.create(uow.session, data)
            return item  # Auto-commits on exit

    # Manual usage in services:
    with UnitOfWork(session) as uow:
        service.create(uow.session, data)
        service.update(uow.session, other_data)
        # Auto-commits on exit if no exception

    # With savepoints for partial rollback:
    with UnitOfWork(session) as uow:
        service.create(uow.session, data)
        try:
            with uow.savepoint():
                service.risky_operation(uow.session)
        except SomeError:
            pass  # Savepoint rolled back, but main transaction continues
        # Main transaction still commits
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ConcurrencyConflict(Exception):
    """Raised when a concurrency conflict is detected.

    This exception indicates that an operation cannot proceed due to
    a conflicting concurrent modification. Common causes:
    - Optimistic lock version mismatch
    - Row locked by another transaction (nowait=True)
    - Stale data detected during update
    """

    def __init__(self, message: str = "Concurrent modification detected"):
        self.message = message
        super().__init__(message)


class UnitOfWork:
    """Transaction boundary abstraction with auto-commit on success.

    Encapsulates a database session with clear transaction semantics:
    - On successful exit (__exit__ with no exception): commits the transaction
    - On exception: rolls back the transaction
    - Supports nested savepoints via savepoint() method

    This removes ambiguity about who owns the transaction and ensures
    consistent commit/rollback behavior across the codebase.

    Attributes:
        session: The SQLAlchemy session for this unit of work.

    Example:
        with UnitOfWork(session, auto_commit=True) as uow:
            entity = Model(name="test")
            uow.session.add(entity)
            uow.session.flush()  # Get ID without committing
            # ... more operations ...
        # Transaction committed here
    """

    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        """Initialize the unit of work.

        Args:
            session: SQLAlchemy session to manage.
            auto_commit: If True (default), commits on successful exit.
                        If False, only flushes (caller must commit).
        """
        self.session = session
        self._auto_commit = auto_commit
        self._entered = False

    def __enter__(self) -> UnitOfWork:
        """Enter the unit of work context."""
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> Literal[False]:
        """Exit the unit of work context.

        On exception: rolls back the transaction.
        On success with auto_commit=True: commits the transaction.

        Returns:
            False (never suppresses exceptions).
        """
        if exc_type is not None:
            logger.debug(
                "Rolling back transaction due to exception: %s",
                exc_val,
            )
            self.session.rollback()
        elif self._auto_commit:
            self.session.commit()
        return False

    @contextmanager
    def savepoint(self) -> Generator[None, None, None]:
        """Create a nested savepoint for partial rollback support.

        Savepoints allow rolling back part of a transaction while keeping
        the rest. Useful for attempting risky operations that may fail
        without aborting the entire transaction.

        Example:
            with UnitOfWork(session) as uow:
                uow.session.add(Entity(name="primary"))
                try:
                    with uow.savepoint():
                        uow.session.add(Entity(name="risky"))
                        raise SomeError()  # This rolls back only the savepoint
                except SomeError:
                    pass
                # "primary" entity is still added, "risky" is rolled back
        """
        nested = self.session.begin_nested()
        try:
            yield
            nested.commit()
        except Exception:
            nested.rollback()
            raise

    def flush(self) -> None:
        """Flush pending changes to the database without committing.

        Useful for getting auto-generated IDs or triggering constraints
        while still within the transaction boundary.
        """
        self.session.flush()

    def refresh(self, instance: object) -> None:
        """Refresh an instance from the database.

        Args:
            instance: The model instance to refresh.
        """
        self.session.refresh(instance)
