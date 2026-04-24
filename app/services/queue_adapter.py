"""Queue boundary for async job dispatch.

Application code should depend on this adapter instead of directly depending on
Celery APIs. The default implementation delegates to the existing Celery helper,
while preserving a small DTO that can also be backed by RabbitMQ, SQS, or a fake
queue in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.services.adapters import adapter_registry


@dataclass(frozen=True)
class QueueMessage:
    task_name: str
    args: tuple[object, ...] = ()
    kwargs: dict[str, object] = field(default_factory=dict)
    queue: str | None = None
    countdown: int | None = None
    eta: datetime | None = None
    correlation_id: str | None = None
    source: str | None = None
    request_id: str | None = None
    actor_id: str | None = None
    headers: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class QueueDispatchResult:
    queued: bool
    task_id: str | None = None
    task_name: str | None = None
    queue: str | None = None
    error: str | None = None


class QueueBackend(Protocol):
    def enqueue(self, message: QueueMessage) -> QueueDispatchResult:
        ...


class CeleryQueueAdapter:
    """Queue adapter backed by the existing Celery application."""

    name = "queue.celery"
    depends_on: tuple[str, ...] = ()  # No dependencies - foundational

    def __init__(self, enqueue_func: Any | None = None) -> None:
        self._enqueue_func = enqueue_func

    def health_check(self) -> tuple[bool, str]:
        """Verify Celery broker connectivity."""
        try:
            from app.celery_app import celery_app

            # Ping the broker with a short timeout
            conn = celery_app.connection()
            conn.ensure_connection(max_retries=1, timeout=5)
            conn.release()
            return True, "Celery broker connection OK"
        except Exception as exc:
            return False, f"Celery broker connection failed: {exc}"

    def enqueue(self, message: QueueMessage) -> QueueDispatchResult:
        enqueue_func = self._enqueue_func
        if enqueue_func is None:
            from app.celery_app import enqueue_celery_task

            enqueue_func = enqueue_celery_task

        apply_async_kwargs: dict[str, object] = {}
        if message.queue:
            apply_async_kwargs["queue"] = message.queue
        if message.countdown is not None:
            apply_async_kwargs["countdown"] = message.countdown
        if message.eta is not None:
            apply_async_kwargs["eta"] = message.eta

        try:
            enqueue_kwargs: dict[str, object] = {
                "args": list(message.args),
                "correlation_id": message.correlation_id,
                "source": message.source,
            }
            if message.kwargs:
                enqueue_kwargs["kwargs"] = message.kwargs
            if message.request_id is not None:
                enqueue_kwargs["request_id"] = message.request_id
            if message.actor_id is not None:
                enqueue_kwargs["actor_id"] = message.actor_id
            if message.headers:
                enqueue_kwargs["headers"] = message.headers
            result = enqueue_func(
                message.task_name,
                **enqueue_kwargs,
                **apply_async_kwargs,
            )
        except Exception as exc:
            return QueueDispatchResult(
                queued=False,
                task_name=message.task_name,
                queue=message.queue,
                error=str(exc),
            )

        return QueueDispatchResult(
            queued=True,
            task_id=str(getattr(result, "id", "") or "") or None,
            task_name=message.task_name,
            queue=message.queue,
        )


queue_adapter = CeleryQueueAdapter()
adapter_registry.register(queue_adapter)


def enqueue_task(
    task_name: str,
    *,
    args: tuple[object, ...] | list[object] | None = None,
    kwargs: dict[str, object] | None = None,
    queue: str | None = None,
    countdown: int | None = None,
    eta: datetime | None = None,
    correlation_id: str | None = None,
    source: str | None = None,
    request_id: str | None = None,
    actor_id: str | None = None,
    headers: dict[str, object] | None = None,
) -> QueueDispatchResult:
    return queue_adapter.enqueue(
        QueueMessage(
            task_name=task_name,
            args=tuple(args or ()),
            kwargs=dict(kwargs or {}),
            queue=queue,
            countdown=countdown,
            eta=eta,
            correlation_id=correlation_id,
            source=source,
            request_id=request_id,
            actor_id=actor_id,
            headers=dict(headers or {}),
        )
    )
