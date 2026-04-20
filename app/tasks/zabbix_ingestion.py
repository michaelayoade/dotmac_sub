from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.celery_app import celery_app
from app.models.catalog import Subscription
from app.services.db_session_adapter import db_session_adapter
from app.services.redis_client import get_redis
from app.services.zabbix import ZabbixClientError, zabbix_configured
from app.services.zabbix_engine import (
    PORTAL_VISIBLE_SERVICE_STATUSES,
    get_zabbix_engine,
)

logger = logging.getLogger(__name__)
_DISPATCH_LOCK_KEY = "zabbix:portal_usage_ingestion:dispatch:lock"
_DISPATCH_CURSOR_KEY = "zabbix:portal_usage_ingestion:dispatch:cursor"
_CHUNK_LOCK_PREFIX = "zabbix:portal_usage_ingestion:chunk"
_LOCK_TTL_SECONDS = 900


def _zabbix_enabled() -> bool:
    return zabbix_configured()


def _chunk_size() -> int:
    try:
        return max(1, int(os.getenv("ZABBIX_PORTAL_USAGE_CHUNK_SIZE", "25")))
    except ValueError:
        return 25


def _max_chunks_per_run() -> int:
    try:
        return max(1, int(os.getenv("ZABBIX_PORTAL_USAGE_MAX_CHUNKS_PER_RUN", "20")))
    except ValueError:
        return 20


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _rotating_window(values: list[Any], start: int, count: int) -> list[Any]:
    if not values:
        return []
    return [values[(start + offset) % len(values)] for offset in range(count)]


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if period == "last":
        end_at = now - timedelta(days=30)
        return end_at - timedelta(days=30), end_at
    return now - timedelta(days=30), now


def _lock_key_for_chunk(subscription_ids: list[str], period: str) -> str:
    digest = hashlib.sha256(
        ",".join(sorted(subscription_ids)).encode("utf-8")
    ).hexdigest()
    return f"{_CHUNK_LOCK_PREFIX}:{period}:{digest}"


def _acquire_lock(
    key: str, ttl_seconds: int = _LOCK_TTL_SECONDS
) -> tuple[Any, str] | None:
    redis = get_redis()
    if redis is None:
        return None
    token = str(uuid.uuid4())
    acquired = redis.set(key, token, nx=True, ex=ttl_seconds)
    if not acquired:
        return None
    return redis, token


def _release_lock(redis: Any, key: str, token: str) -> None:
    try:
        if redis.get(key) == token:
            redis.delete(key)
    except Exception:
        logger.info(
            "zabbix_portal_usage_ingestion_unlock_failed",
            extra={"event": "zabbix_portal_usage_ingestion_unlock_failed"},
        )


@celery_app.task(name="app.tasks.zabbix_ingestion.dispatch_portal_usage_ingestion")
def dispatch_portal_usage_ingestion() -> dict[str, Any]:
    """Queue small read-only Zabbix usage ingestion chunks."""
    if not _zabbix_enabled():
        logger.info(
            "zabbix_portal_usage_ingestion_skipped",
            extra={"event": "zabbix_portal_usage_ingestion_skipped"},
        )
        return {"skipped": "zabbix_token_missing"}

    lock = _acquire_lock(_DISPATCH_LOCK_KEY, ttl_seconds=120)
    if lock is None:
        return {"skipped": "dispatch_already_running"}
    redis, lock_token = lock

    db = db_session_adapter.create_session()
    try:
        rows = db.execute(
            select(Subscription.id)
            .where(Subscription.status.in_(PORTAL_VISIBLE_SERVICE_STATUSES))
            .order_by(Subscription.id)
        ).all()
        subscription_ids = [str(row[0]) for row in rows]
        chunks = _chunks(subscription_ids, _chunk_size())
        planned_chunks = [
            (period, chunk) for period in ("current", "last") for chunk in chunks
        ]
        cursor = 0
        try:
            raw_cursor = redis.get(_DISPATCH_CURSOR_KEY)
            cursor = int(raw_cursor or 0)
        except (TypeError, ValueError):
            cursor = 0
        selected_chunks = _rotating_window(
            planned_chunks,
            cursor,
            min(_max_chunks_per_run(), len(planned_chunks)),
        )
        queued = 0
        for period, chunk in selected_chunks:
            ingest_portal_usage_chunk.delay(chunk, period)
            queued += 1
        if planned_chunks:
            redis.set(_DISPATCH_CURSOR_KEY, (cursor + queued) % len(planned_chunks))
        logger.info(
            "zabbix_portal_usage_dispatch_success",
            extra={"event": "zabbix_portal_usage_dispatch_success"},
        )
        return {
            "subscriptions": len(subscription_ids),
            "chunk_size": _chunk_size(),
            "planned_chunks": len(planned_chunks),
            "queued_chunks": queued,
        }
    finally:
        db.close()
        _release_lock(redis, _DISPATCH_LOCK_KEY, lock_token)


@celery_app.task(
    name="app.tasks.zabbix_ingestion.ingest_portal_usage_chunk",
    soft_time_limit=240,
    time_limit=300,
)
def ingest_portal_usage_chunk(
    subscription_ids: list[str],
    period: str,
) -> dict[str, Any]:
    """Refresh read-only Zabbix usage cache for a bounded subscription chunk."""
    if not _zabbix_enabled():
        return {"skipped": "zabbix_token_missing"}
    normalized_period = "last" if period == "last" else "current"
    normalized_ids = sorted({str(item) for item in subscription_ids if item})
    if not normalized_ids:
        return {"subscriptions": 0, "cached": 0}

    lock_key = _lock_key_for_chunk(normalized_ids, normalized_period)
    lock = _acquire_lock(lock_key)
    if lock is None:
        return {
            "skipped": "chunk_already_running",
            "subscriptions": len(normalized_ids),
        }
    redis, lock_token = lock

    db = db_session_adapter.create_session()
    try:
        start_at, end_at = _period_bounds(normalized_period)
        subscriptions = (
            db.query(Subscription)
            .filter(Subscription.id.in_(normalized_ids))
            .filter(Subscription.status.in_(PORTAL_VISIBLE_SERVICE_STATUSES))
            .all()
        )
        result = get_zabbix_engine().ingest_portal_usage_cache_for_subscriptions(
            db,
            subscriptions,
            normalized_period,
            start_at,
            end_at,
        )
        logger.info(
            "zabbix_portal_usage_chunk_success",
            extra={"event": "zabbix_portal_usage_chunk_success"},
        )
        return {**result, "period": normalized_period}
    except ZabbixClientError as exc:
        logger.warning("zabbix_portal_usage_chunk_failed: %s", exc)
        return {"error": "zabbix_unavailable", "period": normalized_period}
    except SoftTimeLimitExceeded:
        logger.warning("zabbix_portal_usage_chunk_timed_out")
        return {"error": "zabbix_ingestion_timed_out", "period": normalized_period}
    finally:
        db.close()
        _release_lock(redis, lock_key, lock_token)


@celery_app.task(name="app.tasks.zabbix_ingestion.ingest_portal_usage")
def ingest_portal_usage() -> dict[str, Any]:
    """Backward-compatible entrypoint; use dispatcher for chunked ingestion."""
    return dispatch_portal_usage_ingestion()


@celery_app.task(
    name="app.tasks.zabbix_ingestion.ingest_olt_signals_from_zabbix",
    soft_time_limit=300,
    time_limit=360,
)
def ingest_olt_signals_from_zabbix() -> dict[str, Any]:
    """Pull ONT signal data from Zabbix and update database.

    This task replaces the old direct SNMP polling. It:
    1. Fetches latest signal values from Zabbix API
    2. Updates OntUnit records with new signal data
    3. Pushes aggregated metrics to VictoriaMetrics

    Schedule: Every 5 minutes (configured in scheduler_config.py)
    """
    if not _zabbix_enabled():
        return {"skipped": "zabbix_token_missing"}

    db = db_session_adapter.create_session()
    try:
        # Import here to avoid circular imports
        from app.services.network.olt_polling_metrics import (
            push_signal_metrics_to_victoriametrics,
        )
        from app.services.zabbix_data_ingest import ingest_all_olt_signals

        # Step 1: Pull data from Zabbix into database
        ingest_result = ingest_all_olt_signals(db)
        db.commit()

        # Step 2: Push aggregated metrics to VictoriaMetrics
        metrics_count = 0
        try:
            metrics_count = push_signal_metrics_to_victoriametrics(db)
        except Exception as exc:
            logger.warning("signal_metrics_push_failed: %s", exc)

        logger.info(
            "zabbix_signal_ingest_complete",
            extra={
                "event": "zabbix_signal_ingest_complete",
                "olts_processed": ingest_result.olts_processed,
                "onts_updated": ingest_result.onts_updated,
                "metrics_pushed": metrics_count,
                "errors": len(ingest_result.errors),
            },
        )

        return {
            "olts_processed": ingest_result.olts_processed,
            "onts_updated": ingest_result.onts_updated,
            "metrics_pushed": metrics_count,
            "errors": ingest_result.errors[:10],  # Limit error list
        }

    except SoftTimeLimitExceeded:
        logger.warning("zabbix_signal_ingest_timed_out")
        return {"error": "zabbix_signal_ingest_timed_out"}
    except Exception as exc:
        logger.exception("zabbix_signal_ingest_failed")
        return {"error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.zabbix_ingestion.sync_devices_to_zabbix")
def sync_devices_to_zabbix() -> dict[str, Any]:
    """Sync DotMac devices to Zabbix hosts.

    Creates or updates Zabbix hosts for all active OLTs and NAS devices.
    Schedule: Every 5 minutes (configured in scheduler_config.py)
    """
    if not _zabbix_enabled():
        return {"skipped": "zabbix_token_missing"}

    db = db_session_adapter.create_session()
    try:
        from app.services.zabbix_host_sync import sync_all_devices

        result = sync_all_devices(db)
        db.commit()

        logger.info(
            "zabbix_device_sync_complete",
            extra={
                "event": "zabbix_device_sync_complete",
                "olt_created": result["olt"]["created"],
                "olt_updated": result["olt"]["updated"],
                "nas_created": result["nas"]["created"],
                "nas_updated": result["nas"]["updated"],
            },
        )

        return result

    except ZabbixClientError as exc:
        logger.warning("zabbix_device_sync_failed: %s", exc)
        return {"error": "zabbix_unavailable"}
    except Exception as exc:
        logger.exception("zabbix_device_sync_exception")
        return {"error": str(exc)}
    finally:
        db.close()
