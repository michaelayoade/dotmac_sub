import logging
import os
from datetime import timedelta

from app.db import SessionLocal
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import integration as integration_service
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


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
    task = (
        db.query(ScheduledTask)
        .filter(ScheduledTask.task_name == task_name)
        .order_by(ScheduledTask.created_at.desc())
        .first()
    )
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
    if task.name != name:
        task.name = name
        changed = True
    if task.interval_seconds != interval_seconds:
        task.interval_seconds = interval_seconds
        changed = True
    if task.enabled != enabled:
        task.enabled = enabled
        changed = True
    if changed:
        db.commit()


def get_celery_config() -> dict:
    broker = None
    backend = None
    timezone = None
    beat_max_loop_interval = 5
    beat_refresh_seconds = 30
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
    except Exception:
        logger.exception("Failed to load scheduler settings from database.")
    finally:
        session.close()

    broker = (
        broker
        or _env_value("REDIS_URL")
        or "redis://localhost:6379/0"
    )
    backend = (
        backend
        or _env_value("REDIS_URL")
        or "redis://localhost:6379/1"
    )
    timezone = timezone or "UTC"
    config: dict[str, object] = {
        "broker_url": broker,
        "result_backend": backend,
        "timezone": timezone,
    }
    config["beat_max_loop_interval"] = beat_max_loop_interval
    config["beat_refresh_seconds"] = beat_refresh_seconds
    return config


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
        _sync_scheduled_task(
            session,
            name="usage_rating_runner",
            task_name="app.tasks.usage.run_usage_rating",
            enabled=usage_enabled,
            interval_seconds=usage_interval_seconds,
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
            task_name="app.tasks.collections.run_dunning",
            enabled=dunning_enabled,
            interval_seconds=dunning_interval_seconds,
        )
        prepaid_enabled = _effective_bool(
            session,
            SettingDomain.collections,
            "prepaid_enforcement_enabled",
            "PREPAID_ENFORCEMENT_ENABLED",
            True,
        )
        prepaid_interval_seconds = _effective_int(
            session,
            SettingDomain.collections,
            "prepaid_enforcement_interval_seconds",
            "PREPAID_ENFORCEMENT_INTERVAL_SECONDS",
            3600,
        )
        prepaid_interval_seconds = max(prepaid_interval_seconds, 300)
        _sync_scheduled_task(
            session,
            name="prepaid_enforcement_runner",
            task_name="app.tasks.collections.run_prepaid_enforcement",
            enabled=prepaid_enabled,
            interval_seconds=prepaid_interval_seconds,
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
        oauth_refresh_interval_seconds = max(oauth_refresh_interval_seconds, 3600)  # Min: 1 hour
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
                    interval_seconds = int(raw_seconds) if raw_seconds is not None else None
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

        # SNMP interface polling and discovery
        snmp_walk_interval = _resolve_int(
            session, SettingDomain.snmp, "interface_walk_interval_seconds", 300
        )
        snmp_discovery_interval = _resolve_int(
            session, SettingDomain.snmp, "interface_discovery_interval_seconds", 3600
        )
        _sync_scheduled_task(
            session,
            name="snmp_interface_walk",
            task_name="app.tasks.snmp.walk_interfaces",
            enabled=True,
            interval_seconds=max(snmp_walk_interval, 30),
        )
        _sync_scheduled_task(
            session,
            name="snmp_interface_discovery",
            task_name="app.tasks.snmp.discover_interfaces",
            enabled=True,
            interval_seconds=max(snmp_discovery_interval, 60),
        )

        # SLA breach detection - runs every 30 minutes
        sla_breach_enabled = _effective_bool(
            session,
            SettingDomain.workflow,
            "sla_breach_detection_enabled",
            "SLA_BREACH_DETECTION_ENABLED",
            True,
        )
        sla_breach_interval_seconds = _resolve_int(
            session,
            SettingDomain.workflow,
            "sla_breach_detection_interval_seconds",
            1800,
        )
        sla_breach_min_interval = _resolve_int(
            session,
            SettingDomain.workflow,
            "sla_breach_detection_min_interval",
            60,
        )
        sla_breach_interval_seconds = max(sla_breach_interval_seconds, sla_breach_min_interval)
        _sync_scheduled_task(
            session,
            name="sla_breach_detection",
            task_name="app.tasks.workflow.detect_sla_breaches",
            enabled=sla_breach_enabled,
            interval_seconds=sla_breach_interval_seconds,
        )

        # WireGuard VPN maintenance tasks
        wg_log_cleanup_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "wireguard_log_cleanup_enabled",
            "WIREGUARD_LOG_CLEANUP_ENABLED",
            True,
        )
        wg_log_cleanup_interval = _resolve_int(
            session, SettingDomain.network, "wireguard_log_cleanup_interval_seconds", 86400
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
        wg_token_cleanup_interval = max(wg_token_cleanup_interval, 300)  # Min: 5 minutes
        _sync_scheduled_task(
            session,
            name="wireguard_token_cleanup",
            task_name="app.tasks.wireguard.cleanup_expired_tokens",
            enabled=wg_token_cleanup_enabled,
            interval_seconds=wg_token_cleanup_interval,
        )

        # WireGuard peer stats sync - runs every 5 minutes
        wg_stats_sync_enabled = _effective_bool(
            session,
            SettingDomain.network,
            "wireguard_peer_stats_sync_enabled",
            "WIREGUARD_PEER_STATS_SYNC_ENABLED",
            True,
        )
        wg_stats_sync_interval = _resolve_int(
            session,
            SettingDomain.network,
            "wireguard_peer_stats_sync_interval_seconds",
            300,
        )
        wg_stats_sync_interval = max(wg_stats_sync_interval, 60)  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="wireguard_peer_stats_sync",
            task_name="app.tasks.wireguard.sync_peer_stats",
            enabled=wg_stats_sync_enabled,
            interval_seconds=wg_stats_sync_interval,
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
            session, SettingDomain.scheduler, "event_stale_cleanup_interval_seconds", 600
        )
        event_stale_cleanup_interval = max(event_stale_cleanup_interval, 60)  # Min: 1 minute
        _sync_scheduled_task(
            session,
            name="event_stale_cleanup_runner",
            task_name="app.tasks.events.mark_stale_processing_events",
            enabled=event_stale_cleanup_enabled,
            interval_seconds=event_stale_cleanup_interval,
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
            session, SettingDomain.scheduler, "event_old_cleanup_interval_seconds", 86400
        )
        event_old_cleanup_interval = max(event_old_cleanup_interval, 3600)  # Min: 1 hour
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

        tasks = (
            session.query(ScheduledTask)
            .filter(ScheduledTask.enabled.is_(True))
            .all()
        )
        for task in tasks:
            if task.schedule_type != ScheduleType.interval:
                continue
            interval_seconds = max(task.interval_seconds or 0, 1)
            schedule[f"scheduled_task_{task.id}"] = {
                "task": task.task_name,
                "schedule": timedelta(seconds=interval_seconds),
                "args": task.args_json or [],
                "kwargs": task.kwargs_json or {},
            }
    except Exception:
        logger.exception("Failed to build Celery beat schedule.")
    finally:
        session.close()
    return schedule
