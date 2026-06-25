import logging
import os

from celery import Celery, current_task
from celery.signals import (
    beat_init,
    celeryd_after_setup,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    worker_process_init,
)
from kombu import Queue

from app.services.scheduler_config import (
    build_beat_schedule,
    find_unregistered_scheduled_tasks,
    get_celery_config,
)

logger = logging.getLogger(__name__)


def _running_under_pytest() -> bool:
    return bool(os.getenv("PYTEST_VERSION") or os.getenv("PYTEST_CURRENT_TEST"))


def _test_celery_config() -> dict:
    broker = os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL") or "memory://"
    backend = (
        os.getenv("CELERY_RESULT_BACKEND")
        or os.getenv("REDIS_URL")
        or "cache+memory://"
    )
    return {
        "broker_url": broker,
        "result_backend": backend,
        "timezone": os.getenv("CELERY_TIMEZONE", "UTC"),
        "task_always_eager": os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower()
        in {"1", "true", "yes"},
    }


celery_app = Celery("dotmac_sm")
if _running_under_pytest():
    celery_app.conf.update(_test_celery_config())
    celery_app.conf.beat_schedule = {}
else:
    celery_app.conf.update(get_celery_config())
    celery_app.conf.beat_schedule = build_beat_schedule()
celery_app.conf.beat_scheduler = "app.celery_scheduler.DbScheduler"
# How often DbScheduler rebuilds the schedule from the DB. The default 30s
# meant ~2,880 full build_beat_schedule() passes/day (dozens of settings
# queries each) against the cross-host Postgres. Schedule changes are rare;
# 5 min is plenty responsive.
celery_app.conf.beat_refresh_seconds = int(
    os.getenv("CELERY_BEAT_REFRESH_SECONDS", "300")
)
celery_app.autodiscover_tasks(["app.tasks"])

# Route critical OLT authorization and ACS/TR-069 tasks to dedicated queues.
# This prevents ACS work from being starved by slow SNMP/polling/default tasks.
celery_app.conf.task_routes = {
    "app.tasks.tr069.sync_all_acs_devices": {"queue": "acs"},
    "app.tasks.tr069.execute_pending_jobs": {"queue": "acs"},
    "app.tasks.tr069.check_device_health": {"queue": "acs"},
    "app.tasks.tr069.refresh_ont_runtime_data": {"queue": "acs"},
    "app.tasks.tr069.cleanup_tr069_records": {"queue": "acs"},
    "app.tasks.tr069.cleanup_stale_genieacs_tasks": {"queue": "acs"},
    "app.tasks.tr069.scrape_genieacs_metrics": {"queue": "acs"},
    "app.tasks.tr069.execute_bulk_action": {"queue": "acs"},
    "app.tasks.tr069.wait_for_ont_bootstrap": {"queue": "acs"},
    "app.tasks.tr069.apply_saved_ont_service_config": {"queue": "acs"},
    "app.tasks.tr069.apply_acs_config": {"queue": "acs"},
    "app.tasks.ont_provisioning.authorize_ont": {"queue": "tr069"},
    "app.tasks.ont_provisioning.provision_ont": {"queue": "tr069"},
    "app.tasks.ont_provisioning.queue_bulk_provisioning": {"queue": "tr069"},
    "app.tasks.profile_sync.execute_due_profile_sync_tasks": {"queue": "tr069"},
    # High-volume bandwidth tasks - dedicated queue to prevent starvation
    "app.tasks.bandwidth.process_bandwidth_stream": {"queue": "bandwidth"},
    "app.tasks.bandwidth.aggregate_to_metrics": {"queue": "bandwidth"},
    "app.tasks.bandwidth.flush_bandwidth_buffer": {"queue": "bandwidth"},
    # High-volume ingestion tasks - dedicated queue
    "app.tasks.zabbix_ingestion.ingest_portal_usage_chunk": {"queue": "ingestion"},
    "app.tasks.zabbix_ingestion.ingest_portal_usage_batch": {"queue": "ingestion"},
    "app.tasks.topology_sync.run_topology_reconcile": {"queue": "ingestion"},
    "app.tasks.topology_sync.warm_topology_status": {"queue": "ingestion"},
    "app.tasks.topology_lldp.run_lldp_topology_poll": {"queue": "ingestion"},
    "app.tasks.usage.import_radius_accounting": {"queue": "ingestion"},
    "app.tasks.usage.reap_stale_radius_sessions": {"queue": "ingestion"},
    "app.tasks.usage.meter_usage_into_quota": {"queue": "ingestion"},
    # Safety-net FUP reset runs off the billing queue on purpose, so expired
    # throttle/block enforcement is still lifted when the billing queue stalls.
    "app.tasks.usage.lift_expired_fup_enforcement": {"queue": "ingestion"},
    # Operator-triggered identity checks should not wait behind bulk jobs.
    "app.tasks.nin_tasks.verify_nin_task": {"queue": "nin"},
    # Monitoring cache warmer queries Zabbix; keep it off the default queue so
    # it isn't starved by slow/long default-queue jobs.
    "app.tasks.monitoring_warm.warm_monitoring_caches": {"queue": "ingestion"},
    # CRM ticket pull paginates an external API; the default queue's backlog
    # would push it far past its 5-minute schedule.
    "app.tasks.crm_ticket_pull.pull_crm_tickets": {"queue": "crm"},
    "app.tasks.crm_ticket_pull.sync_crm_ticket": {"queue": "crm"},
    "app.tasks.crm_ticket_push.push_ticket_to_crm": {"queue": "crm"},
    "app.tasks.crm_ticket_push.push_comment_to_crm": {"queue": "crm"},
    "app.tasks.crm_billing_push.push_crm_billing_snapshots": {"queue": "crm"},
    "app.tasks.crm_sync.push_subscriber_change": {"queue": "crm"},
    # Daily business runners must not sit behind the default queue's backlog —
    # a buried invoice cycle is a missed billing day (the 2026-06-10 00:55
    # dispatch sat unexecuted behind ~6.6k queued default-queue tasks).
    "app.tasks.billing.run_invoice_cycle": {"queue": "billing"},
    "app.tasks.billing.run_billing_notifications": {"queue": "billing"},
    "app.tasks.autopay.charge_due_invoices": {"queue": "billing"},
    "app.tasks.arrangements.check_overdue_arrangements": {"queue": "billing"},
    "app.tasks.payment_reconciliation.reconcile_topups": {"queue": "billing"},
    "app.tasks.collections.run_billing_enforcement": {"queue": "billing"},
    "app.tasks.collections.run_dunning": {"queue": "billing"},
    "app.tasks.billing.check_billing_switch": {"queue": "billing"},
    "app.tasks.catalog.expire_subscriptions": {"queue": "billing"},
    "app.tasks.enforcement.cleanup_subscription_block_sessions": {"queue": "billing"},
    "app.tasks.usage.run_usage_rating": {"queue": "billing"},
    "app.tasks.usage.evaluate_fup_rules": {"queue": "billing"},
    # Daily customer-facing heads-up; must not sit behind a default-queue
    # backlog or "expires tomorrow" arrives after the bundle already lapsed.
    "app.tasks.usage.notify_expiring_data_bundles": {"queue": "billing"},
    # Read-only enforcement audit; keep with the business runners.
    "app.tasks.radius.audit_suspension_enforcement": {"queue": "billing"},
    # Read-only IPv4 consistency audit; same home as the enforcement audit.
    "app.tasks.radius.audit_ip_consistency": {"queue": "billing"},
}

celery_app.conf.task_queues = (
    Queue("celery"),  # Default queue
    Queue("nin"),  # Dedicated identity verification queue
    Queue("tr069"),  # Dedicated OLT/TR-069 operations queue
    Queue("acs"),  # Dedicated GenieACS/TR-069 queue
    Queue("bandwidth"),  # High-volume bandwidth processing
    Queue("ingestion"),  # High-volume data ingestion (Zabbix, usage)
    Queue("crm"),  # CRM ticket/comment pull (external API paced)
    Queue("billing"),  # Daily business runners (billing/dunning/expiry/FUP)
)

# Ensure all tasks are registered by importing the tasks package
import app.tasks  # noqa: E402, F401
import app.tasks.nin_tasks  # noqa: E402, F401


def _release_metadata() -> dict[str, str | None]:
    return {
        "release": os.getenv("APP_RELEASE")
        or os.getenv("IMAGE_TAG")
        or os.getenv("GIT_SHA"),
        "git_sha": os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA"),
        "environment": os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "unknown",
    }


def _log_release_metadata(component: str) -> None:
    logger.info(
        "application_release",
        extra={
            "event": "application_release",
            "component": component,
            **_release_metadata(),
        },
    )


def _warn_on_scheduler_registry_drift(component: str) -> None:
    try:
        drift = find_unregistered_scheduled_tasks(celery_app.tasks.keys())
    except Exception:
        logger.warning(
            "scheduler_registry_drift_check_failed",
            exc_info=True,
            extra={
                "event": "scheduler_registry_drift_check_failed",
                "component": component,
            },
        )
        return

    if not drift:
        logger.info(
            "scheduler_registry_drift_check_clean",
            extra={
                "event": "scheduler_registry_drift_check_clean",
                "component": component,
            },
        )
        return

    logger.warning(
        "scheduler_registry_drift_detected",
        extra={
            "event": "scheduler_registry_drift_detected",
            "component": component,
            "unknown_task_count": len(drift),
            "unknown_tasks": [item["task_name"] for item in drift],
        },
    )


@worker_process_init.connect
def _dispose_inherited_db_connections(**_kwargs):
    """Celery prefork workers must not reuse parent-created DB connections."""
    from app.db import dispose_engine

    dispose_engine()


@celeryd_after_setup.connect
def _log_worker_boot(**_kwargs):
    _log_release_metadata("celery-worker")
    _warn_on_scheduler_registry_drift("celery-worker")


@beat_init.connect
def _log_beat_boot(**_kwargs):
    _log_release_metadata("celery-beat")
    _warn_on_scheduler_registry_drift("celery-beat")


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
    inherited_correlation_id = getattr(task_request, "correlation_id", None) or getattr(
        task_request, "id", None
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
    # Record a success heartbeat so the billing-health monitor can detect a
    # stalled/dead runner (ScheduledTask.last_run_at is not maintained by beat).
    if state == "SUCCESS" and task is not None and getattr(task, "name", None):
        try:
            from app.services.job_heartbeat import record_success

            record_success(task.name)
        except Exception:
            logger.debug("heartbeat record failed", exc_info=True)


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
