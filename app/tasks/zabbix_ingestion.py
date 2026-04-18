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
from app.db import SessionLocal
from app.models.catalog import Subscription
from app.services.redis_client import get_redis
from app.services.zabbix import ZabbixClientError
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
    return bool(os.getenv("ZABBIX_API_TOKEN"))


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

    db = SessionLocal()
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

    db = SessionLocal()
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
