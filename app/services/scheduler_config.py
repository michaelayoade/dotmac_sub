import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from celery.schedules import crontab

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import integration as integration_service
from app.services.db_session_adapter import db_session_adapter
from app.services.settings_spec import resolve_value
from app.timezone import APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session

TR069_TASK_QUEUE_NAMES = {
    "app.tasks.tr069.sync_all_acs_devices",
    "app.tasks.tr069.execute_pending_jobs",
    "app.tasks.tr069.check_device_health",
    "app.tasks.tr069.refresh_ont_runtime_data",
    "app.tasks.tr069.cleanup_tr069_records",
    "app.tasks.tr069.cleanup_stale_genieacs_tasks",
    "app.tasks.tr069.scrape_genieacs_metrics",
    "app.tasks.tr069.execute_bulk_action",
    "app.tasks.tr069.wait_for_ont_bootstrap",
    "app.tasks.tr069.apply_saved_ont_service_config",
    "app.tasks.tr069.apply_acs_config",
}


def _zabbix_configured_default() -> bool:
    try:
        from app.services.zabbix import zabbix_configured

        return zabbix_configured()
    except Exception:
        logger.debug("zabbix_scheduler_default_resolution_failed", exc_info=True)
        return False


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_bool(name: str) -> bool | None:
    raw = _env_value(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = _env_value(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _get_setting_value(db, domain: SettingDomain, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == domain)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text:
        return str(setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _effective_bool(
    db, domain: SettingDomain, key: str, env_key: str, default: bool
) -> bool:
    # Single control plane: if this (domain, key) is a registered control, the
    # ONE resolver decides — composing env override, the canonical row a
    # registry-driven admin page writes, the legacy alias, and the owning
    # module. Behavior-neutral for registered keys until a module is disabled or
    # a canonical row is set, because each control's on_missing == the legacy
    # default here (asserted by the parity test).
    from app.services import control_registry

    canonical = control_registry.control_for_legacy(domain, key)
    if canonical is not None:
        return control_registry.is_enabled(db, canonical)

    # Unregistered key: legacy env -> DB -> default.
    env_value = _env_bool(env_key)
    if env_value is not None:
        return env_value
    value = _get_setting_value(db, domain, key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _effective_int(
    db, domain: SettingDomain, key: str, env_key: str, default: int
) -> int:
    env_value = _env_int(env_key)
    if env_value is not None:
        return env_value
    value = _get_setting_value(db, domain, key)
    if value is None:
        return default
    try:
        return int(str(value))
    except ValueError:
        return default


def _resolve_int(db, domain: SettingDomain, key: str, default: int) -> int:
    raw = resolve_value(db, domain, key)
    if raw is None:
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _effective_str(
    db, domain: SettingDomain, key: str, env_key: str, default: str | None
) -> str | None:
    env_value = _env_value(env_key)
    if env_value is not None:
        return env_value
    value = _get_setting_value(db, domain, key)
    if value is None:
        return default
    return str(value)


def _sync_scheduled_task(
    db,
    name: str,
    task_name: str,
    enabled: bool,
    interval_seconds: int,
) -> None:
    # Match by NAME (the stable logical identity), not task_name. Matching by
    # task_name meant a task rename/move (e.g. run_dunning -> run_billing_
    # enforcement) found no row and INSERTED a new one, leaving the old row as a
    # duplicate name. Matching by name updates the task_name in place instead.
    tasks = list(
        db.query(ScheduledTask)
        .filter(ScheduledTask.name == name)
        .order_by(ScheduledTask.created_at.desc())
        .all()
    )
    task = tasks[0] if tasks else None
    if not task:
        if not enabled:
            return
        task = ScheduledTask(
            name=name,
            task_name=task_name,
            schedule_type=ScheduleType.interval,
            interval_seconds=interval_seconds,
            enabled=True,
        )
        db.add(task)
        db.commit()
        return
    changed = False
    # Defensive dedupe: delete any stray duplicate rows for this name (the
    # unique constraint prevents new ones, but pre-existing data may have them).
    # Intentional history drop: the surplus rows are hard-deleted; nothing has a
    # FK to scheduled_tasks.id, so no dependent records are affected.
    for duplicate in tasks[1:]:
        db.delete(duplicate)
        changed = True
    if task.task_name != task_name:
        task.task_name = task_name
        changed = True
    if task.interval_seconds != interval_seconds:
        task.interval_seconds = interval_seconds
        changed = True
    if task.enabled != enabled:
        task.enabled = enabled
        changed = True
    if changed:
        db.commit()


def _retire_scheduled_task(db, task_name: str) -> None:
    tasks = db.query(ScheduledTask).filter(ScheduledTask.task_name == task_name).all()
    changed = False
    for task in tasks:
        if task.enabled:
            task.enabled = False
            changed = True
    if changed:
        db.commit()


def find_unregistered_scheduled_tasks(
    registered_task_names: Iterable[str],
) -> list[dict[str, object]]:
    registered = set(registered_task_names)
    session = SessionLocal()
    try:
        tasks = (
            session.query(ScheduledTask)
            .filter(ScheduledTask.enabled.is_(True))
            .order_by(ScheduledTask.name.asc(), ScheduledTask.created_at.asc())
            .all()
        )
        return [
            {
                "id": task.id,
                "name": task.name,
                "task_name": task.task_name,
                "interval_seconds": task.interval_seconds,
            }
            for task in tasks
            if task.task_name not in registered
        ]
    finally:
        session.close()


def get_celery_config() -> dict:
    broker = None
    backend = None
    timezone = None
    beat_max_loop_interval = 5
    beat_refresh_seconds = 30
    task_soft_time_limit = 840
    task_time_limit = 900
    acs_soft_time_limit = 240
    acs_time_limit = 300
    long_soft_time_limit = 1740
    long_time_limit = 1800
    worker_prefetch_multiplier = 1
    session = SessionLocal()
    try:
        broker = _effective_str(
            session, SettingDomain.scheduler, "broker_url", "CELERY_BROKER_URL", None
        )
        backend = _effective_str(
            session,
            SettingDomain.scheduler,
            "result_backend",
            "CELERY_RESULT_BACKEND",
            None,
        )
        timezone = _effective_str(
            session, SettingDomain.scheduler, "timezone", "CELERY_TIMEZONE", None
        )
        beat_max_loop_interval = _effective_int(
            session,
            SettingDomain.scheduler,
            "beat_max_loop_interval",
            "CELERY_BEAT_MAX_LOOP_INTERVAL",
            5,
        )
        beat_refresh_seconds = _effective_int(
            session,
            SettingDomain.scheduler,
            "beat_refresh_seconds",
            "CELERY_BEAT_REFRESH_SECONDS",
            30,
        )
        task_soft_time_limit = _effective_int(
            session,
            SettingDomain.scheduler,
            "task_soft_time_limit_seconds",
            "CELERY_TASK_SOFT_TIME_LIMIT",
            task_soft_time_limit,
        )
        task_time_limit = _effective_int(
            session,
            SettingDomain.scheduler,
            "task_time_limit_seconds",
            "CELERY_TASK_TIME_LIMIT",
            task_time_limit,
        )
        acs_soft_time_limit = _effective_int(
            session,
            SettingDomain.scheduler,
            "acs_task_soft_time_limit_seconds",
            "CELERY_ACS_TASK_SOFT_TIME_LIMIT",
            acs_soft_time_limit,
        )
        acs_time_limit = _effective_int(
            session,
            SettingDomain.scheduler,
            "acs_task_time_limit_seconds",
            "CELERY_ACS_TASK_TIME_LIMIT",
            acs_time_limit,
        )
        long_soft_time_limit = _effective_int(
            session,
            SettingDomain.scheduler,
            "long_task_soft_time_limit_seconds",
            "CELERY_LONG_TASK_SOFT_TIME_LIMIT",
            long_soft_time_limit,
        )
        long_time_limit = _effective_int(
            session,
            SettingDomain.scheduler,
            "long_task_time_limit_seconds",
            "CELERY_LONG_TASK_TIME_LIMIT",
            long_time_limit,
        )
        worker_prefetch_multiplier = _effective_int(
            session,
            SettingDomain.scheduler,
            "worker_prefetch_multiplier",
            "CELERY_WORKER_PREFETCH_MULTIPLIER",
            worker_prefetch_multiplier,
        )
    except Exception:
        logger.exception("Failed to load scheduler settings from database.")
    finally:
        session.close()

    broker = broker or _env_value("REDIS_URL") or "redis://localhost:6379/0"
    backend = backend or _env_value("REDIS_URL") or "redis://localhost:6379/1"
    timezone = timezone or APP_TIMEZONE_NAME
    config: dict[str, object] = {
        "broker_url": broker,
        "result_backend": backend,
        "timezone": timezone,
    }
    config["beat_max_loop_interval"] = beat_max_loop_interval
    config["beat_refresh_seconds"] = beat_refresh_seconds
    config["task_soft_time_limit"] = max(30, task_soft_time_limit)
    config["task_time_limit"] = max(config["task_soft_time_limit"] + 1, task_time_limit)
    config["worker_prefetch_multiplier"] = max(1, worker_prefetch_multiplier)
    config["task_acks_late"] = True
    config["task_reject_on_worker_lost"] = True
    config["task_track_started"] = True
    config["broker_connection_retry_on_startup"] = True
    config["worker_cancel_long_running_tasks_on_connection_loss"] = True
    config["result_expires"] = _env_int("CELERY_RESULT_EXPIRES") or 86400

    acs_limits = {
        "soft_time_limit": max(30, acs_soft_time_limit),
        "time_limit": max(acs_soft_time_limit + 1, acs_time_limit),
    }
    long_limits = {
        "soft_time_limit": max(60, long_soft_time_limit),
        "time_limit": max(long_soft_time_limit + 1, long_time_limit),
    }
    annotations: dict[str, dict[str, int]] = dict.fromkeys(
        TR069_TASK_QUEUE_NAMES, acs_limits
    )
    annotations.update(
        {
            "app.tasks.ont_provisioning.provision_ont": long_limits,
            "app.tasks.ont_provisioning.queue_bulk_provisioning": long_limits,
            "app.tasks.olt_firmware.upgrade_with_verification": long_limits,
            "app.tasks.olt_firmware.rollback": long_limits,
            "app.tasks.provisioning.run_bulk_activation_job": long_limits,
            "app.tasks.provisioning.run_service_migration_job": long_limits,
            # Whole-base daily runs (4k+ subscriptions); the default 900s
            # limit can kill a catch-up billing or dunning pass mid-run.
            "app.tasks.billing.run_invoice_cycle": long_limits,
            "app.tasks.collections.run_billing_enforcement": long_limits,
            "app.tasks.collections.run_dunning": long_limits,
        }
    )
    config["task_annotations"] = annotations
    return config


def _entry_expires_seconds(interval_seconds: int) -> int:
    """Message expiry for a periodic task.

    A periodic message that hasn't been consumed within its own interval is
    obsolete — its successor is already queued behind it. Expiring it lets a
    backlogged worker discard stale duplicates instead of executing hours of
    identical runs back to back (the default queue once held 115 queued copies
    of one cache refresh). Day-or-longer tasks get 12h: still ample room for
    queue latency, but a dead queue can't accumulate days of business runs.
    """
    if interval_seconds >= 86400:
        return 43200
    return interval_seconds


def _interval_to_beat_schedule(task_id, interval_seconds: int):
    """Beat schedule object for an interval task.

    Celery beat measures `timedelta` intervals from its own (non-persisted)
    start time, so a daily task only fires after 24h of uninterrupted beat
    uptime — under frequent deploys it never comes due (this starved billing,
    dunning, expiration and FUP runs for weeks). Day-long intervals are
    therefore anchored to a stable wall-clock slot instead: 00:00-05:59,
    spread deterministically by task id so the daily runners don't stampede.
    Sub-daily intervals keep their timedelta — a restart delays them by at
    most one interval, which is acceptable.
    """
    if 86400 <= interval_seconds < 2 * 86400:
        anchor = task_id.int if hasattr(task_id, "int") else abs(hash(task_id))
        return crontab(minute=anchor % 60, hour=(anchor // 60) % 6)
    if interval_seconds >= 2 * 86400:
        logger.warning(
            "scheduled_task_multiday_interval_restart_relative",
            extra={"task_id": str(task_id), "interval_seconds": interval_seconds},
        )
    return timedelta(seconds=interval_seconds)


def _cron_to_beat_schedule(cron_expr: str | None):
    """Parse a standard 5-field cron expression into a celery ``crontab``.

    Fields are ``minute hour day-of-month month day-of-week``. Returns ``None``
    for a missing/malformed expression (celery validates each field and raises
    on bad syntax), so callers skip the entry rather than crash the scheduler.
    """
    if not cron_expr:
        return None
    fields = str(cron_expr).split()
    if len(fields) != 5:
        return None
    minute, hour, day_of_month, month_of_year, day_of_week = fields
    try:
        return crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        )
    except Exception:
        return None


def is_valid_cron(cron_expr: str | None) -> bool:
    """Whether ``cron_expr`` is a usable 5-field cron expression."""
    return _cron_to_beat_schedule(cron_expr) is not None


def next_cron_run(cron_expr: str | None):
    """Best-effort next fire time (UTC) from now for a cron expr, or None.

    Uses celery's ``crontab.remaining_estimate`` which is relative to the real
    current time, so this is a live preview, not a pure function of an argument.
    """
    sched = _cron_to_beat_schedule(cron_expr)
    if sched is None:
        return None
    try:
        now = datetime.now(UTC)
        return now + sched.remaining_estimate(now)
    except Exception:
        return None


def _scheduled_row_to_entry(task) -> tuple[str, dict] | None:
    """Build a beat-schedule (key, entry) for a ScheduledTask row, or None to skip.

    Honours ``schedule_type``: ``crontab`` rows use ``cron_expr`` (skipped with a
    warning if malformed); ``interval`` rows use the interval anchoring. Pure (no
    DB) so it can be unit-tested against in-memory rows.
    """
    options: dict = {}
    if task.task_name in TR069_TASK_QUEUE_NAMES:
        options["queue"] = "acs"
    if task.schedule_type == ScheduleType.crontab:
        task_schedule = _cron_to_beat_schedule(task.cron_expr)
        if task_schedule is None:
            logger.warning(
                "scheduled_task_invalid_cron",
                extra={
                    "task_id": str(task.id),
                    "task_name": task.task_name,
                    "cron_expr": task.cron_expr,
                },
            )
            return None
    elif task.schedule_type == ScheduleType.interval:
        interval_seconds = max(task.interval_seconds or 0, 1)
        options["expires"] = _entry_expires_seconds(interval_seconds)
        task_schedule = _interval_to_beat_schedule(task.id, interval_seconds)
    else:
        return None
    return f"scheduled_task_{task.id}", {
        "task": task.task_name,
        "schedule": task_schedule,
        "args": task.args_json or [],
        "kwargs": task.kwargs_json or {},
        "options": options,
    }


def build_beat_schedule() -> dict:
    schedule: dict[str, dict] = {}
    session = SessionLocal()
    try:
        enabled = _effective_bool(
            session, SettingDomain.gis, "sync_enabled", "GIS_SYNC_ENABLED", True
        )
        interval_minutes = _effective_int(
            session,
            SettingDomain.gis,
            "sync_interval_minutes",
            "GIS_SYNC_INTERVAL_MINUTES",
            60,
        )
        if enabled:
            schedule["gis_sync"] = {
                "task": "app.tasks.gis.sync_gis_sources",
                "schedule": timedelta(minutes=max(interval_minutes, 1)),
            }
        vas_enabled = _effective_bool(
            session, SettingDomain.vas, "enabled", "VAS_ENABLED", False
        )
        if vas_enabled:
            # Daily sweep; pay_bill settlement is idempotent so re-runs are safe.
            schedule["vas_wallet_auto_deduct"] = {
                "task": "app.tasks.vas.run_wallet_auto_deduct",
                "schedule": timedelta(hours=24),
            }
            schedule["vas_requery"] = {
                "task": "app.tasks.vas.run_vas_requery",
                "schedule": timedelta(minutes=5),
            }
            schedule["vas_catalog_sync"] = {
                "task": "app.tasks.vas.sync_vas_catalog",
                "schedule": timedelta(hours=12),
            }
            schedule["vas_review_requery"] = {
                "task": "app.tasks.vas.run_vas_review_requery",
                "schedule": timedelta(hours=24),
            }
        usage_enabled = _effective_bool(
            session,
            SettingDomain.usage,
            "usage_rating_enabled",
            "USAGE_RATING_ENABLED",
            True,
        )
        usage_interval_seconds = _effective_int(
            session,
            SettingDomain.usage,
            "usage_rating_interval_seconds",
            "USAGE_RATING_INTERVAL_SECONDS",
            86400,
        )
        usage_interval_seconds = max(usage_interval_seconds, 300)
        usage_metering_interval_seconds = _effective_int(
            session,
            SettingDomain.usage,
            "usage_metering_interval_seconds",
            "USAGE_METERING_INTERVAL_SECONDS",
            60,
        )
        usage_metering_interval_seconds = max(usage_metering_interval_seconds, 60)
        fup_evaluation_interval_seconds = _effective_int(
            session,
            SettingDomain.usage,
            "fup_evaluation_interval_seconds",
            "FUP_EVALUATION_INTERVAL_SECONDS",
            60,
        )
        fup_evaluation_interval_seconds = max(fup_evaluation_interval_seconds, 60)
        _sync_scheduled_task(
            session,
            name="usage_rating_runner",
            task_name="app.tasks.usage.run_usage_rating",
            enabled=usage_enabled,
            interval_seconds=usage_interval_seconds,
        )
        radius_accounting_enabled = _effective_bool(
            session,
            SettingDomain.usage,
            "radius_accounting_import_enabled",
            "RADIUS_ACCOUNTING_IMPORT_ENABLED",
            True,
        )
        radius_accounting_interval_seconds = _effective_int(
            session,
            SettingDomain.usage,
            "radius_accounting_import_interval_seconds",
            "RADIUS_ACCOUNTING_IMPORT_INTERVAL_SECONDS",
            60,
        )
        radius_accounting_interval_seconds = max(radius_accounting_interval_seconds, 10)
        _sync_scheduled_task(
            session,
            name="radius_accounting_importer",
            task_name="app.tasks.usage.import_radius_accounting",
            enabled=radius_accounting_enabled,
            interval_seconds=radius_accounting_interval_seconds,
        )
        # Close ghost sessions (no Stop packet, accounting feed silent). Off by
        # default: enable only after the importer's open-session refresh has
        # been running for at least one interim interval, so genuinely live
        # sessions have a fresh last_update_at before the first reap.
        radius_reap_enabled = _effective_bool(
            session,
            SettingDomain.usage,
            "radius_session_reap_enabled",
            "RADIUS_SESSION_REAP_ENABLED",
            False,
        )
        radius_reap_interval_seconds = _effective_int(
            session,
            SettingDomain.usage,
            "radius_session_reap_interval_seconds",
            "RADIUS_SESSION_REAP_INTERVAL_SECONDS",
            900,
        )
        radius_reap_interval_seconds = max(radius_reap_interval_seconds, 60)
        # Daily heads-up before a purchased data bundle lapses (push + email).
        _sync_scheduled_task(
            session,
            name="data_bundle_expiry_notifier",
            task_name="app.tasks.usage.notify_expiring_data_bundles",
            enabled=usage_enabled,
            interval_seconds=86400,
        )
        _sync_scheduled_task(
            session,
            name="radius_session_reaper",
            task_name="app.tasks.usage.reap_stale_radius_sessions",
            enabled=radius_reap_enabled,
            interval_seconds=radius_reap_interval_seconds,
        )
        # Companion to the app-side reaper above: this one closes the *external*
        # FreeRADIUS radacct table (the mirror reaper only touches the app-side
        # RadiusAccountingSession). Same flag/interval — without it, dead NAS
        # leave phantom radacct "online" sessions forever.
        _sync_scheduled_task(
            session,
            name="radacct_ghost_reaper",
            task_name="app.tasks.radius.reap_radacct_ghosts",
            enabled=radius_reap_enabled,
            interval_seconds=radius_reap_interval_seconds,
        )
        # Roll imported RADIUS accounting into quota buckets (feeds FUP/overage).
        # Gated by the same usage flag. This follows the RADIUS accounting
        # cadence instead of the daily usage-rating cadence so FUP decisions
        # are applied within minutes of imported usage.
        _sync_scheduled_task(
            session,
            name="usage_metering_runner",
            task_name="app.tasks.usage.meter_usage_into_quota",
            enabled=usage_enabled,
            interval_seconds=usage_metering_interval_seconds,
        )
        # Evaluate FUP rules against the metered usage and apply / auto-lift
        # throttle/block. Keep this separate from daily usage rating; capped
        # plans need near-real-time enforcement.
        _sync_scheduled_task(
            session,
            name="fup_evaluation_runner",
            task_name="app.tasks.usage.evaluate_fup_rules",
            enabled=usage_enabled,
            interval_seconds=fup_evaluation_interval_seconds,
        )
        # Queue-independent backstop: lift FUP enforcement whose reset boundary
        # has passed even if the billing queue (where evaluate_fup_rules runs) is
        # stalled, so customers aren't left throttled/blocked past their window.
        _sync_scheduled_task(
            session,
            name="fup_reset_safety_net",
            task_name="app.tasks.usage.lift_expired_fup_enforcement",
            enabled=usage_enabled,
            interval_seconds=max(usage_interval_seconds, 300),
        )
        # Queue-independent backstop: lift FUP enforcement whose reset boundary
        # has passed even if the billing queue (where evaluate_fup_rules runs) is
        # stalled, so customers aren't left throttled/blocked past their window.
        _sync_scheduled_task(
            session,
            name="fup_reset_safety_net",
            task_name="app.tasks.usage.lift_expired_fup_enforcement",
            enabled=usage_enabled,
            interval_seconds=max(usage_interval_seconds, 300),
        )
        zabbix_usage_enabled_by_default = _zabbix_configured_default()
        zabbix_usage_enabled = _effective_bool(
            session,
            SettingDomain.usage,
            "zabbix_portal_usage_ingestion_enabled",
            "ZABBIX_PORTAL_USAGE_INGESTION_ENABLED",
            zabbix_usage_enabled_by_default,
        )
        zabbix_usage_interval_seconds = _effective_int(
            session,
            SettingDomain.usage,
            "zabbix_portal_usage_ingestion_interval_seconds",
            "ZABBIX_PORTAL_USAGE_INGESTION_INTERVAL_SECONDS",
            300,
        )
        zabbix_usage_interval_seconds = max(zabbix_usage_interval_seconds, 30)
        _retire_scheduled_task(
            session,
            "app.tasks.zabbix_ingestion.ingest_portal_usage",
        )
        _sync_scheduled_task(
            session,
            name="zabbix_portal_usage_ingestion",
            task_name="app.tasks.zabbix_ingestion.dispatch_portal_usage_ingestion",
            enabled=zabbix_usage_enabled,
            interval_seconds=zabbix_usage_interval_seconds,
        )
        billing_enabled = _effective_bool(
            session,
            SettingDomain.billing,
            "billing_enabled",
            "BILLING_ENABLED",
            True,
        )
        billing_interval_seconds = _effective_int(
            session,
            SettingDomain.billing,
            "billing_interval_seconds",
            "BILLING_INTERVAL_SECONDS",
            86400,
        )
        billing_interval_seconds = max(billing_interval_seconds, 300)
        _sync_scheduled_task(
            session,
            name="billing_runner",
            task_name="app.tasks.billing.run_invoice_cycle",
            enabled=billing_enabled,
            interval_seconds=billing_interval_seconds,
        )
        compensation_retry_enabled = _effective_bool(
            session,
            SettingDomain.provisioning,
            "compensation_retry_enabled",
            "COMPENSATION_RETRY_ENABLED",
            True,
        )
        compensation_retry_interval_seconds = _effective_int(
            session,
            SettingDomain.provisioning,
            "compensation_retry_interval_seconds",
            "COMPENSATION_RETRY_INTERVAL_SECONDS",
            300,
        )
        compensation_retry_interval_seconds = max(
            compensation_retry_interval_seconds,
            60,
        )
        _sync_scheduled_task(
            session,
            name="compensation_retry_watchdog",
            task_name="app.tasks.provisioning.retry_pending_compensation_failures",
            enabled=compensation_retry_enabled,
            interval_seconds=compensation_retry_interval_seconds,
        )
        # Overdue invoice detection — independent of billing cycle
        overdue_enabled = _effective_bool(
            session,
            SettingDomain.billing,
            "overdue_check_enabled",
            "BILLING_OVERDUE_CHECK_ENABLED",
            True,
        )
        overdue_interval = _effective_int(
            session,
            SettingDomain.billing,
            "overdue_check_interval_seconds",
            "BILLING_OVERDUE_CHECK_INTERVAL_SECONDS",
            3600,
        )
        overdue_interval = max(overdue_interval, 300)
        _sync_scheduled_task(
            session,
            name="overdue_checker",
            task_name="app.tasks.billing.mark_invoices_overdue",
            enabled=overdue_enabled,
            interval_seconds=overdue_interval,
        )
        # Dedicated hourly billing-notifications runner. Default OFF: when
        # enabled it owns the reminder/escalation emits and honours the
        # configured send window (billing_notif_send_hour); the daily invoice
        # cycle then skips them. See docs/designs/BILLING_ENFORCEMENT_WINDOW.md.
        billing_notif_hourly_enabled = _effective_bool(
            session,
            SettingDomain.collections,
            "billing_notifications_hourly_enabled",
            "BILLING_NOTIFICATIONS_HOURLY_ENABLED",
            False,
        )
        billing_notif_interval = _effective_int(
            session,
            SettingDomain.collections,
            "billing_notifications_interval_seconds",
            "BILLING_NOTIFICATIONS_INTERVAL_SECONDS",
            3600,
        )
        billing_notif_interval = max(billing_notif_interval, 300)
        _sync_scheduled_task(
            session,
            name="billing_notifications_runner",
            task_name="app.tasks.billing.run_billing_notifications",
            enabled=billing_notif_hourly_enabled,
            interval_seconds=billing_notif_interval,
        )
        # Unified billing enforcement. The legacy setting/key remains
        # ``dunning_enabled`` for operator compatibility, but the scheduled task
        # now routes through the single billing-enforcement reconciler. Accrual
        # remains mode-specific; suspension/restore decisions converge there.
        dunning_enabled = _effective_bool(
            session,
            SettingDomain.collections,
            "dunning_enabled",
            "DUNNING_ENABLED",
            True,
        )
        dunning_interval_seconds = _effective_int(
            session,
            SettingDomain.collections,
            "dunning_interval_seconds",
            "DUNNING_INTERVAL_SECONDS",
            86400,
        )
        dunning_interval_seconds = max(dunning_interval_seconds, 60)
        _sync_scheduled_task(
            session,
            name="dunning_runner",
            task_name="app.tasks.collections.run_billing_enforcement",
            enabled=dunning_enabled,
            interval_seconds=dunning_interval_seconds,
        )
        # Billing master-switch config guard — ALWAYS on (independent of
        # billing_enabled) so an unexpected flip is caught, not silently armed.
        _sync_scheduled_task(
            session,
            name="billing_switch_guard",
            task_name="app.tasks.billing.check_billing_switch",
            enabled=True,
            interval_seconds=3600,
        )
        # Autopay charging (idempotent; due-date gating lives in the service)
        autopay_enabled = _effective_bool(
            session,
            SettingDomain.billing,
            "autopay_enabled",
            "BILLING_AUTOPAY_ENABLED",
            True,
        )
        autopay_interval_seconds = _effective_int(
            session,
            SettingDomain.billing,
            "autopay_interval_seconds",
            "BILLING_AUTOPAY_INTERVAL_SECONDS",
            3600,
        )
        autopay_interval_seconds = max(autopay_interval_seconds, 300)
        _sync_scheduled_task(
            session,
            name="autopay_runner",
            task_name="app.tasks.autopay.charge_due_invoices",
            enabled=autopay_enabled,
            interval_seconds=autopay_interval_seconds,
        )
        # Payment arrangement installment due/overdue/default checks (daily)
        arrangement_check_enabled = _effective_bool(
            session,
            SettingDomain.collections,
            "arrangement_check_enabled",
            "ARRANGEMENT_CHECK_ENABLED",
            True,
        )
        arrangement_check_interval_seconds = _effective_int(
            session,
            SettingDomain.collections,
            "arrangement_check_interval_seconds",
            "ARRANGEMENT_CHECK_INTERVAL_SECONDS",
            86400,
        )
        arrangement_check_interval_seconds = max(
            arrangement_check_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="arrangement_overdue_checker",
            task_name="app.tasks.arrangements.check_overdue_arrangements",
            enabled=arrangement_check_enabled,
            interval_seconds=arrangement_check_interval_seconds,
        )
        # Sweep stranded top-up intents against the gateway verify API
        topup_reconciliation_enabled = _effective_bool(
            session,
            SettingDomain.billing,
            "topup_reconciliation_enabled",
            "BILLING_TOPUP_RECONCILIATION_ENABLED",
            True,
        )
        topup_reconciliation_interval_seconds = _effective_int(
            session,
            SettingDomain.billing,
            "topup_reconciliation_interval_seconds",
            "BILLING_TOPUP_RECONCILIATION_INTERVAL_SECONDS",
            1800,
        )
        topup_reconciliation_interval_seconds = max(
            topup_reconciliation_interval_seconds, 300
        )
        _sync_scheduled_task(
            session,
            name="topup_reconciliation_runner",
            task_name="app.tasks.payment_reconciliation.reconcile_topups",
            enabled=topup_reconciliation_enabled,
            interval_seconds=topup_reconciliation_interval_seconds,
        )
        # Suspension-enforcement audit — read-only check that fully-blocked
        # subscribers are actually unreachable in the external RADIUS DB.
        suspension_audit_enabled = _effective_bool(
            session,
            SettingDomain.radius,
            "suspension_audit_enabled",
            "RADIUS_SUSPENSION_AUDIT_ENABLED",
            True,
        )
        suspension_audit_interval_seconds = _effective_int(
            session,
            SettingDomain.radius,
            "suspension_audit_interval_seconds",
            "RADIUS_SUSPENSION_AUDIT_INTERVAL_SECONDS",
            21600,  # Every 6 hours
        )
        suspension_audit_interval_seconds = max(suspension_audit_interval_seconds, 900)
        _sync_scheduled_task(
            session,
            name="radius_suspension_audit",
            task_name="app.tasks.radius.audit_suspension_enforcement",
            enabled=suspension_audit_enabled,
            interval_seconds=suspension_audit_interval_seconds,
        )
        # IPv4 consistency audit (read-only; quantifies column/IPAM/radreply
        # drift). Shares cadence defaults with the suspension audit.
        ip_consistency_audit_enabled = _effective_bool(
            session,
            SettingDomain.radius,
            "ip_consistency_audit_enabled",
            "RADIUS_IP_CONSISTENCY_AUDIT_ENABLED",
            True,
        )
        ip_consistency_audit_interval_seconds = _effective_int(
            session,
            SettingDomain.radius,
            "ip_consistency_audit_interval_seconds",
            "RADIUS_IP_CONSISTENCY_AUDIT_INTERVAL_SECONDS",
            21600,  # Every 6 hours
        )
        ip_consistency_audit_interval_seconds = max(
            ip_consistency_audit_interval_seconds, 900
        )
        _sync_scheduled_task(
            session,
            name="radius_ip_consistency_audit",
            task_name="app.tasks.radius.audit_ip_consistency",
            enabled=ip_consistency_audit_enabled,
            interval_seconds=ip_consistency_audit_interval_seconds,
        )
        # Connectivity shadow audit (read-only full-base sweep; quantifies
        # desired-vs-actual drift per dimension — the cutover-readiness gauge for
        # the connectivity reconciler). Shares the audit cadence defaults.
        connectivity_shadow_audit_enabled = _effective_bool(
            session,
            SettingDomain.radius,
            "connectivity_shadow_audit_enabled",
            "RADIUS_CONNECTIVITY_SHADOW_AUDIT_ENABLED",
            True,
        )
        connectivity_shadow_audit_interval_seconds = _effective_int(
            session,
            SettingDomain.radius,
            "connectivity_shadow_audit_interval_seconds",
            "RADIUS_CONNECTIVITY_SHADOW_AUDIT_INTERVAL_SECONDS",
            21600,  # Every 6 hours
        )
        connectivity_shadow_audit_interval_seconds = max(
            connectivity_shadow_audit_interval_seconds, 900
        )
        _sync_scheduled_task(
            session,
            name="connectivity_shadow_audit",
            task_name="app.tasks.radius.connectivity_shadow_audit",
            enabled=connectivity_shadow_audit_enabled,
            interval_seconds=connectivity_shadow_audit_interval_seconds,
        )
        # Subscription expiration enforcement (runs daily)
        subscription_expiration_enabled = _effective_bool(
            session,
            SettingDomain.catalog,
            "subscription_expiration_enabled",
            "SUBSCRIPTION_EXPIRATION_ENABLED",
            True,
        )
        subscription_expiration_interval_seconds = _effective_int(
            session,
            SettingDomain.catalog,
            "subscription_expiration_interval_seconds",
            "SUBSCRIPTION_EXPIRATION_INTERVAL_SECONDS",
            86400,  # Daily
        )
        subscription_expiration_interval_seconds = max(
            subscription_expiration_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="subscription_expiration_runner",
            task_name="app.tasks.catalog.expire_subscriptions",
            enabled=subscription_expiration_enabled,
            interval_seconds=subscription_expiration_interval_seconds,
        )
        # Infrastructure availability snapshot - daily rollup powering the
        # performance/SLA trend charts (see INFRASTRUCTURE_SLA_PERFORMANCE.md).
        # Safe to run regardless of SLA_AVAILABILITY_LOG_ENABLED: it records the
        # day's per-element availability (PON ONT-online ratio + alert-derived
        # device/site uptime), accumulating trend history either way.
        infra_availability_snapshot_enabled = _effective_bool(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_snapshot_enabled",
            "INFRA_AVAILABILITY_SNAPSHOT_ENABLED",
            True,
        )
        infra_availability_snapshot_interval_seconds = _effective_int(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_snapshot_interval_seconds",
            "INFRA_AVAILABILITY_SNAPSHOT_INTERVAL_SECONDS",
            86400,  # Daily
        )
        infra_availability_snapshot_interval_seconds = max(
            infra_availability_snapshot_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="infra_availability_snapshot",
            task_name=(
                "app.tasks.infrastructure_availability."
                "snapshot_infrastructure_availability"
            ),
            enabled=infra_availability_snapshot_enabled,
            interval_seconds=infra_availability_snapshot_interval_seconds,
        )
        # Infrastructure availability snapshot retention prune (daily).
        infra_availability_prune_enabled = _effective_bool(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_prune_enabled",
            "INFRA_AVAILABILITY_PRUNE_ENABLED",
            True,
        )
        infra_availability_prune_interval_seconds = _effective_int(
            session,
            SettingDomain.network_monitoring,
            "infra_availability_prune_interval_seconds",
            "INFRA_AVAILABILITY_PRUNE_INTERVAL_SECONDS",
            86400,  # Daily
        )
        infra_availability_prune_interval_seconds = max(
            infra_availability_prune_interval_seconds, 3600
        )
        _sync_scheduled_task(
            session,
            name="infra_availability_prune",
            task_name=(
                "app.tasks.infrastructure_availability."
                "prune_infrastructure_availability"
            ),
            enabled=infra_availability_prune_enabled,
            interval_seconds=infra_availability_prune_interval_seconds,
        )
        # Vacation hold auto-resume - runs hourly to resume expired holds
        vacation_hold_resume_enabled = _effective_bool(
            session,
            SettingDomain.catalog,
            "vacation_hold_auto_resume_enabled",
            "VACATION_HOLD_AUTO_RESUME_ENABLED",
            True,
        )
        vacation_hold_resume_interval = _effective_int(
            session,
            SettingDomain.catalog,
            "vacation_hold_resume_interval_seconds",
            "VACATION_HOLD_RESUME_INTERVAL_SECONDS",
            3600,  # Hourly
        )
        vacation_hold_resume_interval = max(vacation_hold_resume_interval, 300)
        _sync_scheduled_task(
            session,
            name="vacation_hold_auto_resume",
            task_name="app.tasks.vacation_holds.resume_expired_holds",
            enabled=vacation_hold_resume_enabled,
            interval_seconds=vacation_hold_resume_interval,
        )
        notification_queue_enabled = _effective_bool(
            session,
            SettingDomain.notification,
            "notification_queue_enabled",
            "NOTIFICATION_QUEUE_ENABLED",
            True,
        )
        notification_queue_interval_seconds = _effective_int(
            session,
            SettingDomain.notification,
            "notification_queue_interval_seconds",
            "NOTIFICATION_QUEUE_INTERVAL_SECONDS",
            60,
        )
        notification_queue_interval_seconds = max(
            notification_queue_interval_seconds, 30
        )
        _sync_scheduled_task(
            session,
            name="notification_queue_runner",
            task_name="app.tasks.notifications.deliver_notification_queue",
            enabled=notification_queue_enabled,
            interval_seconds=notification_queue_interval_seconds,
        )
        retention_enabled = _effective_bool(
            session,
            SettingDomain.catalog,
            "nas_backup_retention_enabled",
            "NAS_BACKUP_RETENTION_ENABLED",
            True,
        )
        retention_interval_seconds = _resolve_int(
            session,
            SettingDomain.provisioning,
            "nas_backup_retention_interval_seconds",
            86400,
        )
        retention_interval_seconds = max(retention_interval_seconds, 3600)
        _sync_scheduled_task(
            session,
            name="nas_backup_retention_cleanup",
            task_name="app.tasks.nas.cleanup_nas_backups",
            enabled=retention_enabled,
            interval_seconds=retention_interval_seconds,
        )
        # OAuth token refresh - runs daily to proactively refresh expiring tokens
        oauth_refresh_enabled = _effective_bool(
            session,
            SettingDomain.comms,
            "oauth_token_refresh_enabled",
            "OAUTH_TOKEN_REFRESH_ENABLED",
            True,
        )
        oauth_refresh_interval_seconds = _resolve_int(
            session,
            SettingDomain.provisioning,
            "oauth_token_refresh_interval_seconds",
            86400,
        )
        oauth_refresh_interval_seconds = max(
            oauth_refresh_interval_seconds, 3600
        )  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="oauth_token_refresh",
            task_name="app.tasks.oauth.refresh_expiring_tokens",
            enabled=oauth_refresh_enabled,
            interval_seconds=oauth_refresh_interval_seconds,
        )
        integration_jobs = integration_service.list_interval_jobs(session)
        if not integration_jobs:
            logger.info("EMAIL_POLL_EXIT reason=no_jobs")
        for job in integration_jobs:
            # Be defensive: tests may use MagicMock jobs and production may have
            # string values depending on where the job was sourced.
            raw_seconds = getattr(job, "interval_seconds", None)
            if isinstance(raw_seconds, (int, float, str)):
                try:
                    interval_seconds = (
                        int(raw_seconds) if raw_seconds is not None else None
                    )
                except (TypeError, ValueError):
                    interval_seconds = None
            else:
                interval_seconds = None

            if interval_seconds is None:
                raw_minutes = getattr(job, "interval_minutes", None)
                if isinstance(raw_minutes, (int, float, str)):
                    try:
                        minutes = int(raw_minutes) if raw_minutes is not None else 0
                    except (TypeError, ValueError):
                        minutes = 0
                else:
                    minutes = 0
                if minutes:
                    interval_seconds = minutes * 60

            interval_seconds = max(interval_seconds or 0, 1)
            schedule[f"integration_job_{job.id}"] = {
                "task": "app.tasks.integrations.run_integration_job",
                "schedule": timedelta(seconds=interval_seconds),
                "args": [str(job.id)],
            }

        # Bandwidth monitoring tasks
        bandwidth_enabled = _effective_bool(
            session,
            SettingDomain.usage,
            "bandwidth_processing_enabled",
            "BANDWIDTH_PROCESSING_ENABLED",
            True,
        )
        if bandwidth_enabled:
            # Process bandwidth stream - runs every 5 seconds
            bandwidth_stream_interval = _resolve_int(
                session, SettingDomain.bandwidth, "stream_interval_seconds", 5
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_stream_processor",
                task_name="app.tasks.bandwidth.process_bandwidth_stream",
                enabled=bandwidth_enabled,
                interval_seconds=max(bandwidth_stream_interval, 1),
            )

            # Aggregate to VictoriaMetrics - runs every minute
            aggregate_interval = _resolve_int(
                session, SettingDomain.bandwidth, "aggregate_interval_seconds", 60
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_aggregate_to_metrics",
                task_name="app.tasks.bandwidth.aggregate_to_metrics",
                enabled=bandwidth_enabled,
                interval_seconds=max(aggregate_interval, 10),
            )

            # Cleanup hot data - runs hourly
            cleanup_interval = _resolve_int(
                session, SettingDomain.bandwidth, "cleanup_interval_seconds", 3600
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_cleanup_hot_data",
                task_name="app.tasks.bandwidth.cleanup_hot_data",
                enabled=bandwidth_enabled,
                interval_seconds=max(cleanup_interval, 60),
            )

            # Trim Redis stream - runs every 10 minutes
            trim_interval = _resolve_int(
                session, SettingDomain.bandwidth, "trim_interval_seconds", 600
            )
            _sync_scheduled_task(
                session,
                name="bandwidth_trim_stream",
                task_name="app.tasks.bandwidth.trim_redis_stream",
                enabled=bandwidth_enabled,
                interval_seconds=max(trim_interval, 60),
            )

        # Monitoring-path coverage refresh — caches the reachable-CIDR set (from
        # up WireGuard peers) so operational status + the SLA bridge can tell a
        # blind spot from a real outage without running wg on the request path.
        coverage_enabled = _effective_bool(
            session,
            SettingDomain.network_monitoring,
            "monitoring_coverage_enabled",
            "MONITORING_COVERAGE_ENABLED",
            True,
        )
        coverage_interval_seconds = _effective_int(
            session,
            SettingDomain.network_monitoring,
            "monitoring_coverage_interval_seconds",
            "MONITORING_COVERAGE_INTERVAL_SECONDS",
            600,
        )
        _sync_scheduled_task(
            session,
            name="monitoring_coverage_refresh",
            task_name="app.tasks.monitoring_coverage.refresh_monitoring_coverage",
            enabled=coverage_enabled,
            interval_seconds=max(coverage_interval_seconds, 120),
        )

        # OLT health retry - auto-retry failed ping connections
        olt_health_retry_minutes = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "olt_health_retry_interval_minutes",
            5,
        )
        _sync_scheduled_task(
            session,
            name="olt_health_retry",
            task_name="app.tasks.olt_health_retry.retry_failed_olt_connections",
            enabled=True,
            interval_seconds=max(olt_health_retry_minutes * 60, 60),
        )

        # Reap provisioning runs stuck in 'running' (worker died mid-run) so
        # they reach a terminal status instead of blocking dedup + order
        # advancement forever.
        _sync_scheduled_task(
            session,
            name="provisioning_run_reaper",
            task_name="app.tasks.provisioning.reap_stale_provisioning_runs",
            enabled=True,
            interval_seconds=600,
        )

        # ONT telemetry ingest from centralized monitoring data.
        ont_signal_minutes = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "ont_signal_ingest_interval_minutes",
            15,
        )
        _sync_scheduled_task(
            session,
            name="ont_signal_ingest",
            task_name="app.tasks.zabbix_ingestion.ingest_olt_signals_from_zabbix",
            enabled=True,
            interval_seconds=max(ont_signal_minutes * 60, 120),
        )
        _sync_scheduled_task(
            session,
            name="ont_signal_ingest_watchdog",
            task_name="app.tasks.zabbix_ingestion.repair_stale_olt_signal_ingest",
            enabled=True,
            interval_seconds=600,
        )
        # Topology reconcile: pull Zabbix groups/hosts onto pop_sites +
        # network_devices. Device graph changes slowly, so hourly is ample.
        topology_reconcile_minutes = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "topology_reconcile_interval_minutes",
            60,
        )
        _sync_scheduled_task(
            session,
            name="topology_reconcile",
            task_name="app.tasks.topology_sync.run_topology_reconcile",
            enabled=True,
            interval_seconds=max(topology_reconcile_minutes * 60, 300),
        )
        # Warm cached live status for topology nodes (read by the Network Path
        # panel). Default 180s, matching the monitoring dashboard cache TTL.
        topology_status_seconds = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "topology_status_warm_interval_seconds",
            180,
        )
        _sync_scheduled_task(
            session,
            name="topology_status_warm",
            task_name="app.tasks.topology_sync.warm_topology_status",
            enabled=True,
            interval_seconds=max(topology_status_seconds, 60),
        )
        # LLDP neighbor poll -> directed device graph. Physical adjacency changes
        # rarely; hourly is ample.
        lldp_poll_minutes = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "topology_lldp_poll_interval_minutes",
            60,
        )
        _sync_scheduled_task(
            session,
            name="topology_lldp_poll",
            task_name="app.tasks.topology_lldp.run_lldp_topology_poll",
            enabled=True,
            interval_seconds=max(lldp_poll_minutes * 60, 300),
        )
        dashboard_cache_seconds = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "dashboard_cache_refresh_interval_seconds",
            180,
        )
        _sync_scheduled_task(
            session,
            name="dashboard_cache_refresh",
            task_name="app.tasks.app_cache.refresh_dashboard_stats_cache",
            enabled=True,
            interval_seconds=max(dashboard_cache_seconds, 60),
        )
        # Default 120s, below the 180s snapshot cache TTL, so the key is
        # re-warmed before it lapses (a 180s==180s interval let it expire just
        # before the next write, exposing cold-cache reads).
        ont_snapshot_cache_seconds = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "ont_snapshot_cache_refresh_interval_seconds",
            120,
        )
        _sync_scheduled_task(
            session,
            name="ont_snapshot_cache_refresh",
            task_name="app.tasks.app_cache.refresh_ont_zabbix_snapshot_cache",
            enabled=True,
            interval_seconds=max(ont_snapshot_cache_seconds, 60),
        )
        # Keep the per-OLT Zabbix summary cache the monitoring dashboard reads
        # hot. Default 120s, below the 180s snapshot/summary cache TTL, so a
        # viewer never lands on a cold cache and pays the live per-OLT fan-out.
        monitoring_summary_warm_seconds = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "monitoring_summary_warm_interval_seconds",
            120,
        )
        _sync_scheduled_task(
            session,
            name="monitoring_summary_cache_warm",
            task_name="app.tasks.monitoring_warm.warm_monitoring_caches",
            enabled=True,
            interval_seconds=max(monitoring_summary_warm_seconds, 30),
        )
        _retire_scheduled_task(
            session,
            "app.tasks.ont_discovery.discover_all_olt_onts",
        )
        # Periodic SSH autofind polling has been replaced by syslog-based discovery.
        # Retire any existing scheduled task to disable it.
        _retire_scheduled_task(
            session,
            "app.tasks.ont_autofind.discover_all_olt_autofind",
        )

        # OLT config backup (SSH-based running config retrieval)
        olt_backup_hours = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "olt_backup_interval_hours",
            24,
        )
        _sync_scheduled_task(
            session,
            name="olt_config_backup",
            task_name="app.tasks.olt_config_backup.backup_all_olts",
            enabled=True,
            interval_seconds=max(olt_backup_hours * 3600, 3600),
        )
        # Router config backup (REST /export snapshots). Mirrors OLT backup —
        # the keystone for DR/rollback/change-history and for reading firewall/
        # CoA posture from stored config. The task self-gates to online routers.
        router_backup_hours = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "router_config_backup_interval_hours",
            24,
        )
        _sync_scheduled_task(
            session,
            name="router_config_backup",
            task_name="router_sync.capture_scheduled_snapshots",
            enabled=True,
            interval_seconds=max(router_backup_hours * 3600, 3600),
        )
        # NAS config backup orchestrator. The task itself honors each device's
        # backup_enabled + backup_schedule, so this just needs to run often
        # enough to catch due devices (hourly).
        nas_backup_interval = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "nas_config_backup_interval_seconds",
            3600,
        )
        _sync_scheduled_task(
            session,
            name="nas_config_backup",
            task_name="app.tasks.nas.run_scheduled_backups",
            enabled=True,
            interval_seconds=max(nas_backup_interval, 900),
        )
        olt_profile_sync_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "olt_profile_sync_worker_enabled",
            "OLT_PROFILE_SYNC_WORKER_ENABLED",
            False,
        )
        olt_profile_sync_interval_seconds = _effective_int(
            session,
            SettingDomain.network,
            "olt_profile_sync_interval_seconds",
            "OLT_PROFILE_SYNC_INTERVAL_SECONDS",
            300,
        )
        _sync_scheduled_task(
            session,
            name="olt_profile_sync_due_task_runner",
            task_name="app.tasks.profile_sync.execute_due_profile_sync_tasks",
            enabled=olt_profile_sync_enabled,
            interval_seconds=max(olt_profile_sync_interval_seconds, 60),
        )

        for removed_task_name in (
            "app.tasks.olt_capture.capture_olt_samples_task",
            "app.tasks.olt_capture.validate_all_parsers_task",
            "app.tasks.olt_capture.capture_all_olts_task",
            "app.tasks.provisioning_enforcement.run_enforcement",
        ):
            _retire_scheduled_task(session, removed_task_name)

        _retire_scheduled_task(session, "app.tasks.workflow.detect_sla_breaches")

        # WireGuard VPN maintenance tasks
        wg_log_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "wireguard_log_cleanup_enabled",
            "WIREGUARD_LOG_CLEANUP_ENABLED",
            True,
        )
        wg_log_cleanup_interval = _resolve_int(
            session,
            SettingDomain.network,
            "wireguard_log_cleanup_interval_seconds",
            86400,
        )
        wg_log_cleanup_interval = max(wg_log_cleanup_interval, 3600)  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="wireguard_log_cleanup",
            task_name="app.tasks.wireguard.cleanup_connection_logs",
            enabled=wg_log_cleanup_enabled,
            interval_seconds=wg_log_cleanup_interval,
        )

        # WireGuard token cleanup - runs hourly
        wg_token_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "wireguard_token_cleanup_enabled",
            "WIREGUARD_TOKEN_CLEANUP_ENABLED",
            True,
        )
        wg_token_cleanup_interval = _resolve_int(
            session,
            SettingDomain.network,
            "wireguard_token_cleanup_interval_seconds",
            3600,
        )
        wg_token_cleanup_interval = max(
            wg_token_cleanup_interval, 300
        )  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="wireguard_token_cleanup",
            task_name="app.tasks.wireguard.cleanup_expired_tokens",
            enabled=wg_token_cleanup_enabled,
            interval_seconds=wg_token_cleanup_interval,
        )

        _retire_scheduled_task(session, "app.tasks.wireguard.sync_peer_stats")

        # TR-069 ACS device sync - syncs devices from GenieACS
        tr069_sync_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_sync_enabled",
            "TR069_SYNC_ENABLED",
            True,
        )
        tr069_sync_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_sync_interval_seconds",
            1800,  # 30 minutes
        )
        tr069_sync_interval = max(tr069_sync_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_device_sync",
            task_name="app.tasks.tr069.sync_all_acs_devices",
            enabled=tr069_sync_enabled,
            interval_seconds=tr069_sync_interval,
        )

        # TR-069 job execution - executes queued jobs and retries failed
        tr069_jobs_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_job_execution_enabled",
            "TR069_JOB_EXECUTION_ENABLED",
            True,
        )
        tr069_jobs_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_job_execution_interval_seconds",
            60,  # 1 minute
        )
        tr069_jobs_interval = max(tr069_jobs_interval, 30)  # Min: 30 seconds
        _sync_scheduled_task(
            session,
            name="tr069_job_executor",
            task_name="app.tasks.tr069.execute_pending_jobs",
            enabled=tr069_jobs_enabled,
            interval_seconds=tr069_jobs_interval,
        )

        # TR-069 device health check - monitors last_inform freshness
        tr069_health_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_health_check_enabled",
            "TR069_HEALTH_CHECK_ENABLED",
            True,
        )
        tr069_health_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_health_check_interval_seconds",
            7200,  # 2 hours
        )
        tr069_health_interval = max(tr069_health_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_health_checker",
            task_name="app.tasks.tr069.check_device_health",
            enabled=tr069_health_enabled,
            interval_seconds=tr069_health_interval,
        )

        # TR-069 record cleanup - deletes old sessions and jobs
        tr069_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_cleanup_enabled",
            "TR069_CLEANUP_ENABLED",
            True,
        )
        tr069_cleanup_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_cleanup_interval_seconds",
            86400,  # Daily
        )
        tr069_cleanup_interval = max(tr069_cleanup_interval, 3600)  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="tr069_record_cleanup",
            task_name="app.tasks.tr069.cleanup_tr069_records",
            enabled=tr069_cleanup_enabled,
            interval_seconds=tr069_cleanup_interval,
        )

        # TR-069 GenieACS stale task cleanup - deletes stuck tasks/faults older than threshold
        # This prevents inform blocking loops from permanently failing tasks
        tr069_genieacs_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_genieacs_stale_cleanup_enabled",
            "TR069_GENIEACS_STALE_CLEANUP_ENABLED",
            True,
        )
        tr069_genieacs_cleanup_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_genieacs_stale_cleanup_interval_seconds",
            900,  # 15 minutes
        )
        tr069_genieacs_cleanup_interval = max(
            tr069_genieacs_cleanup_interval, 300
        )  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_genieacs_stale_cleanup",
            task_name="app.tasks.tr069.cleanup_stale_genieacs_tasks",
            enabled=tr069_genieacs_cleanup_enabled,
            interval_seconds=tr069_genieacs_cleanup_interval,
        )

        # TR-069 GenieACS metrics scrape - pushes pending/faults/inform-age to VictoriaMetrics
        tr069_metrics_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_metrics_scrape_enabled",
            "TR069_METRICS_SCRAPE_ENABLED",
            True,
        )
        tr069_metrics_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_metrics_scrape_interval_seconds",
            300,  # 5 minutes
        )
        tr069_metrics_interval = max(tr069_metrics_interval, 60)  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="tr069_metrics_scrape",
            task_name="app.tasks.tr069.scrape_genieacs_metrics",
            enabled=tr069_metrics_enabled,
            interval_seconds=tr069_metrics_interval,
        )

        # TR-069 ONT runtime refresh - updates observed WAN/WiFi/LAN data
        tr069_runtime_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "tr069_ont_runtime_refresh_enabled",
            "TR069_ONT_RUNTIME_REFRESH_ENABLED",
            True,
        )
        tr069_runtime_interval = _resolve_int(
            session,
            SettingDomain.network,
            "tr069_ont_runtime_refresh_interval_seconds",
            900,  # 15 minutes
        )
        tr069_runtime_interval = max(tr069_runtime_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="tr069_ont_runtime_refresh",
            task_name="app.tasks.tr069.refresh_ont_runtime_data",
            enabled=tr069_runtime_enabled,
            interval_seconds=tr069_runtime_interval,
        )

        # Authorization only registers the ONT. Online-silent ACS repair remains
        # a manual/operator recovery task only; do not run it as a periodic
        # provisioning loop.
        _retire_scheduled_task(session, "app.tasks.tr069.heal_online_silent_onts")
        _retire_scheduled_task(
            session,
            "app.tasks.ont_verification.verify_ont_provisioning_state",
        )

        # Event retry - retries failed event handlers
        event_retry_enabled = _effective_bool(
            session,
            SettingDomain.scheduler,
            "event_retry_enabled",
            "EVENT_RETRY_ENABLED",
            True,
        )
        event_retry_interval = _resolve_int(
            session, SettingDomain.scheduler, "event_retry_interval_seconds", 300
        )
        event_retry_interval = max(event_retry_interval, 60)  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="event_retry_runner",
            task_name="app.tasks.events.retry_failed_events",
            enabled=event_retry_enabled,
            interval_seconds=event_retry_interval,
        )

        # Event stale processing cleanup - marks stuck events as failed
        event_stale_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.scheduler,
            "event_stale_cleanup_enabled",
            "EVENT_STALE_CLEANUP_ENABLED",
            True,
        )
        event_stale_cleanup_interval = _resolve_int(
            session,
            SettingDomain.scheduler,
            "event_stale_cleanup_interval_seconds",
            600,
        )
        event_stale_cleanup_interval = max(
            event_stale_cleanup_interval, 60
        )  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="event_stale_cleanup_runner",
            task_name="app.tasks.events.mark_stale_processing_events",
            enabled=event_stale_cleanup_enabled,
            interval_seconds=event_stale_cleanup_interval,
        )

        stale_infra_check_enabled = _effective_bool(
            session,
            SettingDomain.scheduler,
            "stale_infrastructure_check_enabled",
            "STALE_INFRASTRUCTURE_CHECK_ENABLED",
            True,
        )
        stale_infra_check_interval = _resolve_int(
            session,
            SettingDomain.scheduler,
            "stale_infrastructure_check_interval_seconds",
            300,
        )
        stale_infra_check_interval = max(stale_infra_check_interval, 60)
        _sync_scheduled_task(
            session,
            name="stale_infrastructure_check",
            task_name="app.tasks.monitoring_cleanup.check_stale_infrastructure",
            enabled=stale_infra_check_enabled,
            interval_seconds=stale_infra_check_interval,
        )

        # Event old cleanup - removes old completed events
        event_old_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.scheduler,
            "event_old_cleanup_enabled",
            "EVENT_OLD_CLEANUP_ENABLED",
            True,
        )
        event_old_cleanup_interval = _resolve_int(
            session,
            SettingDomain.scheduler,
            "event_old_cleanup_interval_seconds",
            86400,
        )
        event_old_cleanup_interval = max(
            event_old_cleanup_interval, 3600
        )  # Min: 1 hour
        _sync_scheduled_task(
            session,
            name="event_old_cleanup_runner",
            task_name="app.tasks.events.cleanup_old_events",
            enabled=event_old_cleanup_enabled,
            interval_seconds=event_old_cleanup_interval,
        )

        # RADIUS sync - syncs NAS devices and users to FreeRADIUS
        radius_sync_enabled = _effective_bool(
            session,
            SettingDomain.radius,
            "radius_sync_enabled",
            "RADIUS_SYNC_ENABLED",
            True,
        )
        radius_sync_interval = _resolve_int(
            session, SettingDomain.radius, "radius_sync_interval_seconds", 300
        )
        radius_sync_interval = max(radius_sync_interval, 60)  # Min: 1 minute
        if radius_sync_enabled:
            # Get all active sync jobs and schedule them
            from app.models.radius import RadiusSyncJob

            sync_jobs = (
                session.query(RadiusSyncJob)
                .filter(RadiusSyncJob.is_active.is_(True))
                .all()
            )
            for sync_job in sync_jobs:
                schedule[f"radius_sync_{sync_job.id}"] = {
                    "task": "app.tasks.radius.run_radius_sync_job",
                    "schedule": timedelta(seconds=radius_sync_interval),
                    "args": [str(sync_job.id)],
                }

        # RADIUS refresh safety-net. radcheck/radreply rebuilds are normally
        # enqueued fire-and-forget on block/restore events; if that enqueue is
        # lost (worker down, broker hiccup, restart) a paid customer can stay
        # walled-gardened until the next event touches them. This periodic
        # whole-table rebuild converges radcheck/radreply on the authoritative
        # subscription/subscriber status, so a dropped enqueue self-heals within
        # one interval. Idempotent (per-user DELETE+INSERT). (cutover fix)
        radius_refresh_safety_enabled = _effective_bool(
            session,
            SettingDomain.subscriber,
            "radius_refresh_safety_net_enabled",
            "RADIUS_REFRESH_SAFETY_NET_ENABLED",
            True,
        )
        radius_refresh_safety_interval = _effective_int(
            session,
            SettingDomain.subscriber,
            "radius_refresh_safety_net_interval_minutes",
            "RADIUS_REFRESH_SAFETY_NET_INTERVAL_MINUTES",
            15,
        )
        radius_refresh_safety_interval = max(radius_refresh_safety_interval, 5)
        if radius_refresh_safety_enabled:
            schedule["radius_refresh_safety_net"] = {
                "task": "app.tasks.radius_population.refresh_radius_from_subs",
                "schedule": timedelta(minutes=radius_refresh_safety_interval),
            }

        # CRM ticket pull: inbound CRM tickets/comments into local support tickets.
        crm_ticket_pull_enabled = _effective_bool(
            session,
            SettingDomain.scheduler,
            "crm_ticket_pull_enabled",
            "CRM_TICKET_PULL_ENABLED",
            False,
        )
        crm_ticket_pull_interval = _effective_int(
            session,
            SettingDomain.scheduler,
            "crm_ticket_pull_interval_minutes",
            "CRM_TICKET_PULL_INTERVAL_MINUTES",
            5,
        )
        crm_ticket_pull_interval = max(crm_ticket_pull_interval, 1)
        if crm_ticket_pull_enabled:
            schedule["crm_ticket_pull"] = {
                "task": "app.tasks.crm_ticket_pull.pull_crm_tickets",
                "schedule": timedelta(minutes=crm_ticket_pull_interval),
            }
            # Daily full reconciliation: heals drift the incremental runs
            # can't see (CRM comments don't bump ticket updated_at; closed
            # tickets are excluded from the incremental comment sweep).
            schedule["crm_ticket_pull_full"] = {
                "task": "app.tasks.crm_ticket_pull.pull_crm_tickets",
                "schedule": crontab(hour=3, minute=40),
                "kwargs": {"full": True},
            }

        # Nightly billing snapshot to the CRM (balance / next bill date /
        # billing cycle on the CRM subscriber record for support agents).
        crm_billing_push_enabled = _effective_bool(
            session,
            SettingDomain.scheduler,
            "crm_billing_push_enabled",
            "CRM_BILLING_PUSH_ENABLED",
            False,
        )
        if crm_billing_push_enabled:
            schedule["crm_billing_push"] = {
                "task": "app.tasks.crm_billing_push.push_crm_billing_snapshots",
                "schedule": crontab(hour=2, minute=30),
            }

        # Daily re-drive of CRM push dead-letters — a multi-hour CRM outage
        # self-recovers without manual action. Runs whenever CRM sync is on
        # (gated by the same ticket-pull flag, the canonical CRM-enabled
        # signal). Cheap no-op when the dead-letter table is empty.
        if crm_ticket_pull_enabled or crm_billing_push_enabled:
            schedule["crm_dead_letter_redrive"] = {
                "task": "app.tasks.crm_sync.redrive_crm_dead_letters",
                "schedule": crontab(hour=4, minute=10),
            }

        # NOTE: the OLT deferred-operations queue + SSH circuit-breaker
        # subsystem was removed (it was never wired — the queue had no
        # producers and the real write paths bypassed the breaker, so it gave
        # false confidence). OLT writes happen directly; reconciliation/cleanup
        # is handled by the ONT reconcile sweeper. The inert schema
        # (queued_olt_operations table + OLTDevice circuit_* columns) was
        # dropped in migration 162.

        # Zabbix device sync - syncs OLT/NAS devices to Zabbix hosts
        zabbix_sync_enabled_by_default = _zabbix_configured_default()
        zabbix_device_sync_enabled = _effective_bool(
            session,
            SettingDomain.network_monitoring,
            "zabbix_device_sync_enabled",
            "ZABBIX_DEVICE_SYNC_ENABLED",
            zabbix_sync_enabled_by_default,
        )
        zabbix_device_sync_interval = _resolve_int(
            session,
            SettingDomain.network_monitoring,
            "zabbix_device_sync_interval_seconds",
            300,  # 5 minutes
        )
        zabbix_device_sync_interval = max(
            zabbix_device_sync_interval, 60
        )  # Min: 1 minute
        # Retire the old un-time-limited copy that lived in zabbix_ingestion; the
        # surviving task in zabbix_sync carries soft/hard time limits.
        _retire_scheduled_task(
            session, "app.tasks.zabbix_ingestion.sync_devices_to_zabbix"
        )
        _sync_scheduled_task(
            session,
            name="zabbix_device_sync",
            task_name="app.tasks.zabbix_sync.sync_devices_to_zabbix",
            enabled=zabbix_device_sync_enabled,
            interval_seconds=zabbix_device_sync_interval,
        )
        _sync_scheduled_task(
            session,
            name="infrastructure_admin_alert_evaluation",
            task_name="app.tasks.admin_alerts.evaluate_infrastructure_alerts",
            enabled=True,
            interval_seconds=60,
        )

        tasks = (
            session.query(ScheduledTask).filter(ScheduledTask.enabled.is_(True)).all()
        )
        for task in tasks:
            entry = _scheduled_row_to_entry(task)
            if entry is not None:
                schedule[entry[0]] = entry[1]
    except Exception:
        logger.exception("Failed to build Celery beat schedule.")
    finally:
        session.close()
    return schedule
