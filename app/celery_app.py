import logging

from celery import Celery
from celery import current_task
from celery.signals import task_failure, task_postrun, task_prerun, task_retry

from app.services.scheduler_config import build_beat_schedule, get_celery_config

logger = logging.getLogger(__name__)

celery_app = Celery("dotmac_sm")
celery_app.conf.update(get_celery_config())
celery_app.conf.beat_schedule = build_beat_schedule()
celery_app.conf.beat_scheduler = "app.celery_scheduler.DbScheduler"
celery_app.autodiscover_tasks(["app.tasks"])

# Route critical TR-069 tasks to a dedicated queue for priority processing.
# This prevents TR-069 sync from being starved by slow SNMP/polling tasks.
celery_app.conf.task_routes = {
    "app.tasks.tr069.sync_all_acs_devices": {"queue": "tr069"},
    "app.tasks.tr069.execute_pending_jobs": {"queue": "tr069"},
    "app.tasks.tr069.check_device_health": {"queue": "tr069"},
    "app.tasks.tr069.refresh_ont_runtime_data": {"queue": "tr069"},
    "app.tasks.tr069.cleanup_tr069_records": {"queue": "tr069"},
    "app.tasks.tr069.execute_bulk_action": {"queue": "tr069"},
}

# Define queues
from kombu import Queue
celery_app.conf.task_queues = (
    Queue("celery"),  # Default queue
    Queue("tr069"),   # Dedicated TR-069 queue
)

# Ensure all tasks are registered by importing the tasks package
import app.tasks  # noqa: E402, F401


def _task_extra(task, task_id: str | None, **extra):
    request = getattr(task, "request", None)
    payload = {
        "event": "celery_task",
        "task_id": task_id,
        "task_name": getattr(task, "name", None),
        "root_id": getattr(request, "root_id", None),
        "parent_id": getattr(request, "parent_id", None),
        "correlation_id": getattr(request, "correlation_id", None),
        "retries": getattr(request, "retries", None),
        "eta": str(getattr(request, "eta", None))
        if getattr(request, "eta", None) is not None
        else None,
    }
    for key, value in extra.items():
        payload[key] = value
    return payload


def _build_enqueue_headers(
    *,
    correlation_id: str | None = None,
    source: str | None = None,
    request_id: str | None = None,
    actor_id: str | None = None,
    headers: dict[str, object] | None = None,
) -> dict[str, object]:
    merged = dict(headers or {})
    task_request = getattr(current_task, "request", None)
    inherited_correlation_id = (
        getattr(task_request, "correlation_id", None) or getattr(task_request, "id", None)
    )
    inherited_request_id = getattr(task_request, "request_id", None)
    inherited_actor_id = getattr(task_request, "actor_id", None)
    if correlation_id or inherited_correlation_id:
        merged["correlation_id"] = correlation_id or inherited_correlation_id
    if source:
        merged["source"] = source
    if request_id or inherited_request_id:
        merged["request_id"] = request_id or inherited_request_id
    if actor_id or inherited_actor_id:
        merged["actor_id"] = actor_id or inherited_actor_id
    return merged


def enqueue_celery_task(
    task_or_name,
    *,
    args: list | tuple | None = None,
    kwargs: dict | None = None,
    correlation_id: str | None = None,
    source: str | None = None,
    request_id: str | None = None,
    actor_id: str | None = None,
    headers: dict[str, object] | None = None,
    **apply_async_kwargs,
):
    task_args = list(args or [])
    task_kwargs = dict(kwargs or {})
    task_headers = _build_enqueue_headers(
        correlation_id=correlation_id,
        source=source,
        request_id=request_id,
        actor_id=actor_id,
        headers=headers,
    )
    task_name: str | None = None
    if isinstance(task_or_name, str):
        result = celery_app.send_task(
            task_or_name,
            args=task_args,
            kwargs=task_kwargs,
            headers=task_headers,
            **apply_async_kwargs,
        )
        task_name = task_or_name
    else:
        result = task_or_name.apply_async(
            args=task_args,
            kwargs=task_kwargs,
            headers=task_headers,
            **apply_async_kwargs,
        )
        task_name = getattr(task_or_name, "name", None)
    logger.info(
        "celery_task_queued",
        extra={
            "event": "celery_task_queue",
            "task_id": str(getattr(result, "id", None)),
            "task_name": task_name,
            "correlation_id": task_headers.get("correlation_id"),
            "request_id": task_headers.get("request_id"),
            "actor_id": task_headers.get("actor_id"),
            "source": task_headers.get("source"),
            "arg_count": len(task_args),
            "kwarg_keys": sorted(task_kwargs.keys()),
        },
    )
    return result


@task_prerun.connect
def _log_task_prerun(task_id=None, task=None, args=None, kwargs=None, **_kwargs):
    logger.info(
        "celery_task_start",
        extra=_task_extra(
            task,
            task_id,
            arg_count=len(args or ()),
            kwarg_keys=sorted((kwargs or {}).keys()),
        ),
    )


@task_postrun.connect
def _log_task_postrun(task_id=None, task=None, state=None, retval=None, **_kwargs):
    logger.info(
        "celery_task_complete",
        extra=_task_extra(
            task,
            task_id,
            task_state=state,
            result_type=type(retval).__name__ if retval is not None else None,
        ),
    )


@task_failure.connect
def _log_task_failure(task_id=None, exception=None, sender=None, einfo=None, **_kwargs):
    task = sender
    logger.error(
        "celery_task_failed",
        extra=_task_extra(
            task,
            task_id,
            error=str(exception) if exception is not None else None,
            exception_type=type(exception).__name__ if exception is not None else None,
        ),
        exc_info=einfo.exc_info if einfo is not None else None,
    )


@task_retry.connect
def _log_task_retry(request=None, reason=None, einfo=None, **_kwargs):
    task = getattr(request, "task", None)
    logger.warning(
        "celery_task_retry",
        extra=_task_extra(
            task,
            getattr(request, "id", None),
            error=str(reason) if reason is not None else None,
            exception_type=type(reason).__name__ if reason is not None else None,
        ),
        exc_info=einfo.exc_info if einfo is not None else None,
    )
