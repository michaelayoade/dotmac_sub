"""Task idempotency decorator for Celery tasks.

Provides the @idempotent_task decorator that prevents duplicate execution
of tasks based on an idempotency key derived from task arguments.

Usage:
    from app.services.task_idempotency import idempotent_task

    @celery_app.task(name="app.tasks.payments.process_payment")
    @idempotent_task(key_func=lambda payment_id: f"payment:{payment_id}")
    def process_payment(payment_id: str) -> dict:
        # This will only run once per payment_id
        ...

    # Using key_params for simple parameter-based keys:
    @celery_app.task(name="app.tasks.billing.generate_invoice")
    @idempotent_task(key_params=["subscription_id", "billing_period"])
    def generate_invoice(subscription_id: str, billing_period: str) -> dict:
        # Key: "generate_invoice:subscription_id=X:billing_period=Y"
        ...
"""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from celery import current_task
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.task_execution import TaskExecution, TaskExecutionStatus
from app.services.db_session_adapter import db_session_adapter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# Default timeout for considering a "running" task as stale
DEFAULT_STALE_TIMEOUT = timedelta(hours=1)


class TaskAlreadyRunning(Exception):
    """Raised when a task with the same idempotency key is already running."""

    def __init__(self, idempotency_key: str, existing_task_id: str | None = None):
        self.idempotency_key = idempotency_key
        self.existing_task_id = existing_task_id
        super().__init__(
            f"Task with key {idempotency_key} is already running"
            + (f" (task_id: {existing_task_id})" if existing_task_id else "")
        )


class TaskAlreadySucceeded(Exception):
    """Raised when a task with the same idempotency key already succeeded."""

    def __init__(self, idempotency_key: str, result: dict | None = None):
        self.idempotency_key = idempotency_key
        self.result = result
        super().__init__(f"Task with key {idempotency_key} already succeeded")


def _build_idempotency_key(
    task_name: str,
    key_func: Callable[..., str] | None,
    key_params: list[str] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Build the idempotency key from function arguments.

    Args:
        task_name: Name of the Celery task.
        key_func: Optional function to compute key from args/kwargs.
        key_params: Optional list of parameter names to include in key.
        args: Positional arguments passed to task.
        kwargs: Keyword arguments passed to task.

    Returns:
        The idempotency key string.
    """
    if key_func is not None:
        custom_key = key_func(*args, **kwargs)
        return f"{task_name}:{custom_key}"

    if key_params:
        # Build key from specified parameters
        # This requires inspecting the function signature to map args to names
        parts = [task_name]
        for param in sorted(key_params):
            if param in kwargs:
                parts.append(f"{param}={kwargs[param]}")
        return ":".join(parts)

    # Default: hash all arguments
    arg_str = f"args={args!r}:kwargs={sorted(kwargs.items())!r}"
    arg_hash = hashlib.sha256(arg_str.encode()).hexdigest()[:16]
    return f"{task_name}:{arg_hash}"


def _get_or_create_execution(
    db,
    idempotency_key: str,
    task_name: str,
    celery_task_id: str | None,
    stale_timeout: timedelta,
) -> tuple[TaskExecution | None, bool]:
    """Get existing execution or create new one.

    Returns:
        Tuple of (execution, is_new). If is_new is True, the caller should
        proceed with task execution. If is_new is False, check execution.status
        to determine whether to skip or return cached result.
    """
    # First, try to find existing execution
    stmt = select(TaskExecution).where(TaskExecution.idempotency_key == idempotency_key)
    existing = db.scalars(stmt).first()

    if existing is not None:
        # Check if it's a stale "running" execution
        if existing.status == TaskExecutionStatus.running:
            stale_threshold = datetime.now(UTC) - stale_timeout
            # Handle timezone-naive datetimes from SQLite
            created_at = existing.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at < stale_threshold:
                # Mark stale execution as failed and allow retry
                logger.warning(
                    "Marking stale task execution as failed: %s (created: %s)",
                    idempotency_key,
                    existing.created_at,
                )
                existing.status = TaskExecutionStatus.failed
                existing.error_message = "Marked as failed due to stale timeout"
                existing.completed_at = datetime.now(UTC)
                db.flush()
                # Fall through to create new execution
            else:
                return existing, False

        # If succeeded or recently failed, return existing
        if existing.status in (
            TaskExecutionStatus.succeeded,
            TaskExecutionStatus.failed,
        ):
            return existing, False

    # Create new execution
    execution = TaskExecution(
        idempotency_key=idempotency_key,
        task_name=task_name,
        status=TaskExecutionStatus.running,
        celery_task_id=celery_task_id,
    )
    db.add(execution)

    try:
        db.flush()
        return execution, True
    except IntegrityError:
        # Race condition: another process created the execution
        db.rollback()
        existing = db.scalars(stmt).first()
        return existing, False


def idempotent_task(
    key_func: Callable[..., str] | None = None,
    key_params: list[str] | None = None,
    stale_timeout: timedelta = DEFAULT_STALE_TIMEOUT,
    skip_if_running: bool = True,
    return_cached_result: bool = True,
) -> Callable[[Callable[P, R]], Callable[P, R | dict[str, Any]]]:
    """Decorator to make a Celery task idempotent.

    Prevents duplicate execution of a task by tracking executions in the
    database. On subsequent calls with the same idempotency key:
    - If previous execution is still running: skip (or raise if skip_if_running=False)
    - If previous execution succeeded: return cached result
    - If previous execution failed: allow retry

    Args:
        key_func: Function to compute idempotency key from task arguments.
                 Receives the same args/kwargs as the task.
        key_params: List of parameter names to include in the idempotency key.
                   Simpler alternative to key_func for parameter-based keys.
        stale_timeout: How long a "running" task can be before it's considered
                      stale and eligible for retry. Default 1 hour.
        skip_if_running: If True (default), return early when task is already
                        running. If False, raise TaskAlreadyRunning.
        return_cached_result: If True (default), return cached result for
                             succeeded tasks. If False, raise TaskAlreadySucceeded.

    Returns:
        Decorated task function.

    Example:
        @celery_app.task(name="app.tasks.payments.charge")
        @idempotent_task(key_func=lambda payment_id, **kw: f"charge:{payment_id}")
        def charge_payment(payment_id: str, amount: int) -> dict:
            # Only executes once per payment_id
            return {"charged": amount}
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R | dict[str, Any]]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | dict[str, Any]:
            # Get task name from Celery context or function name
            task_request = getattr(current_task, "request", None)
            task_name = (
                getattr(current_task, "name", None)
                or getattr(func, "name", None)
                or func.__name__
            )
            celery_task_id = getattr(task_request, "id", None)

            # Build idempotency key
            idempotency_key = _build_idempotency_key(
                task_name, key_func, key_params, args, kwargs
            )

            db = db_session_adapter.create_session()
            try:
                execution, is_new = _get_or_create_execution(
                    db,
                    idempotency_key,
                    task_name,
                    celery_task_id,
                    stale_timeout,
                )

                if not is_new and execution is not None:
                    if execution.status == TaskExecutionStatus.running:
                        if skip_if_running:
                            logger.info(
                                "Skipping task %s: already running (key=%s)",
                                task_name,
                                idempotency_key,
                            )
                            return {
                                "skipped": True,
                                "reason": "already_running",
                                "existing_task_id": execution.celery_task_id,
                            }
                        raise TaskAlreadyRunning(
                            idempotency_key, execution.celery_task_id
                        )

                    if execution.status == TaskExecutionStatus.succeeded:
                        if return_cached_result:
                            logger.info(
                                "Returning cached result for task %s (key=%s)",
                                task_name,
                                idempotency_key,
                            )
                            return execution.result or {"cached": True}
                        raise TaskAlreadySucceeded(idempotency_key, execution.result)

                    # Status is failed - allow retry by creating new execution
                    execution, is_new = _get_or_create_execution(
                        db,
                        idempotency_key + f":retry:{datetime.now(UTC).isoformat()}",
                        task_name,
                        celery_task_id,
                        stale_timeout,
                    )

                db.commit()

                # Execute the actual task
                try:
                    result = func(*args, **kwargs)

                    # Mark as succeeded
                    if execution is not None:
                        execution.status = TaskExecutionStatus.succeeded
                        execution.result = (
                            result if isinstance(result, dict) else {"result": result}
                        )
                        execution.completed_at = datetime.now(UTC)
                        db.commit()

                    return result

                except Exception as exc:
                    # Mark as failed
                    if execution is not None:
                        execution.status = TaskExecutionStatus.failed
                        execution.error_message = str(exc)[
                            :4000
                        ]  # Truncate long errors
                        execution.completed_at = datetime.now(UTC)
                        db.commit()
                    raise

            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        return wrapper

    return decorator


def cleanup_old_executions(
    db,
    *,
    max_age_days: int = 30,
    batch_size: int = 1000,
) -> int:
    """Clean up old task execution records.

    Removes completed (succeeded or failed) task executions older than
    max_age_days. Running tasks are never removed.

    Args:
        db: Database session.
        max_age_days: Remove executions older than this many days.
        batch_size: Maximum records to delete per call.

    Returns:
        Number of records deleted.
    """
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)

    stmt = (
        delete(TaskExecution)
        .where(TaskExecution.status != TaskExecutionStatus.running)
        .where(TaskExecution.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )

    # Use LIMIT if supported (PostgreSQL specific)
    result = db.execute(stmt)
    deleted = result.rowcount
    db.commit()

    if deleted > 0:
        logger.info("Cleaned up %d old task execution records", deleted)

    return deleted
