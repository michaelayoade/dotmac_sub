"""
Celery tasks for bandwidth data processing.

These tasks consume the Redis stream produced by the poller service,
insert samples into PostgreSQL, and push aggregates to VictoriaMetrics.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import redis
from celery import shared_task
from sqlalchemy import delete, func

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.bandwidth import BandwidthSample
from app.models.domain_settings import SettingDomain
from app.services.metrics_store import BandwidthPoint, get_metrics_store
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_STREAM = os.getenv("BANDWIDTH_REDIS_STREAM", "bandwidth:samples")

# Default values for fallback
_DEFAULT_BATCH_SIZE = 1000
_DEFAULT_HOT_RETENTION_HOURS = 24
_DEFAULT_REDIS_STREAM_MAX_LENGTH = 100000
_DEFAULT_REDIS_READ_TIMEOUT_MS = 1000


def _parse_int_setting(value: object | None, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            return default
    return default


def _get_batch_size(db=None) -> int:
    """Get batch size from settings."""
    size = resolve_value(db, SettingDomain.bandwidth, "batch_size") if db else None
    return _parse_int_setting(size, _DEFAULT_BATCH_SIZE)


def _get_hot_retention_hours(db=None) -> int:
    """Get hot data retention hours from settings."""
    hours = resolve_value(db, SettingDomain.bandwidth, "hot_retention_hours") if db else None
    return _parse_int_setting(hours, _DEFAULT_HOT_RETENTION_HOURS)


def _get_redis_stream_max_length(db=None) -> int:
    """Get Redis stream max length from settings."""
    length = resolve_value(db, SettingDomain.bandwidth, "redis_stream_max_length") if db else None
    return _parse_int_setting(length, _DEFAULT_REDIS_STREAM_MAX_LENGTH)


def _get_redis_read_timeout_ms(db=None) -> int:
    """Get Redis read timeout in ms from settings."""
    timeout = resolve_value(db, SettingDomain.bandwidth, "redis_read_timeout_ms") if db else None
    return _parse_int_setting(timeout, _DEFAULT_REDIS_READ_TIMEOUT_MS)


def _get_redis_client():
    """Get a synchronous Redis client."""
    return redis.from_url(REDIS_URL)


@celery_app.task(name="app.tasks.bandwidth.process_bandwidth_stream")
def process_bandwidth_stream():
    """
    Consume samples from the Redis stream and insert into PostgreSQL.

    This task is designed to run frequently (every 5 seconds) and process
    batches of bandwidth samples from the Redis stream.
    """
    r = _get_redis_client()
    db = SessionLocal()

    try:
        # Get configurable settings
        batch_size = _get_batch_size(db)
        read_timeout_ms = _get_redis_read_timeout_ms(db)

        # Read batch from stream
        # Use consumer group for reliability
        group_name = "bandwidth_processor"
        consumer_name = "worker_1"

        # Create consumer group if it doesn't exist
        try:
            r.xgroup_create(REDIS_STREAM, group_name, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        # Read pending messages first (unacked from previous runs)
        pending = r.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={REDIS_STREAM: "0"},
            count=batch_size,
            block=0,
        )

        # Then read new messages
        new_messages = r.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={REDIS_STREAM: ">"},
            count=batch_size,
            block=read_timeout_ms,
        )

        all_messages = []
        if pending:
            all_messages.extend(pending[0][1])
        if new_messages:
            all_messages.extend(new_messages[0][1])

        if not all_messages:
            return {"processed": 0}

        # Parse and insert samples
        samples = []
        message_ids = []

        for msg_id, data in all_messages:
            message_ids.append(msg_id)

            try:
                sample_at = datetime.fromisoformat(data[b"sample_at"].decode())
                samples.append(BandwidthSample(
                    subscription_id=UUID(data[b"subscription_id"].decode()),
                    device_id=UUID(data[b"nas_device_id"].decode()) if data.get(b"nas_device_id") else None,
                    rx_bps=int(data[b"rx_bps"]),
                    tx_bps=int(data[b"tx_bps"]),
                    sample_at=sample_at,
                ))
            except Exception as e:
                logger.error(f"Failed to parse sample {msg_id}: {e}")

        # Bulk insert samples
        if samples:
            db.bulk_save_objects(samples)
            db.commit()

        # Acknowledge processed messages
        if message_ids:
            r.xack(REDIS_STREAM, group_name, *message_ids)

        logger.info(f"Processed {len(samples)} bandwidth samples")
        return {"processed": len(samples)}

    except Exception as e:
        logger.error(f"Error processing bandwidth stream: {e}")
        db.rollback()
        raise
    finally:
        db.close()
        r.close()


@celery_app.task(name="app.tasks.bandwidth.cleanup_hot_data")
def cleanup_hot_data():
    """
    Remove bandwidth samples older than the retention period.

    Hot data (raw samples) is kept in PostgreSQL for the configured retention hours,
    after which it's deleted. Aggregated data is retained in VictoriaMetrics.
    """
    db = SessionLocal()
    retention_hours = _get_hot_retention_hours(db)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)

    try:
        result = db.execute(
            delete(BandwidthSample).where(BandwidthSample.sample_at < cutoff)
        )
        deleted = result.rowcount
        db.commit()

        logger.info(f"Cleaned up {deleted} bandwidth samples older than {cutoff}")
        return {"deleted": deleted}

    except Exception as e:
        logger.error(f"Error cleaning up hot data: {e}")
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.bandwidth.aggregate_to_metrics")
def aggregate_to_metrics():
    """
    Calculate aggregates from hot data and push to VictoriaMetrics.

    This task runs every minute and calculates 1-minute aggregates
    (avg, max) for each subscription, then pushes them to VictoriaMetrics.
    """
    db = SessionLocal()

    try:
        # Calculate aggregates for the last minute
        now = datetime.now(timezone.utc)
        minute_start = now.replace(second=0, microsecond=0)
        minute_end = minute_start + timedelta(minutes=1)

        # Query aggregates grouped by subscription
        aggregates = (
            db.query(
                BandwidthSample.subscription_id,
                BandwidthSample.device_id,
                func.avg(BandwidthSample.rx_bps).label("rx_avg"),
                func.avg(BandwidthSample.tx_bps).label("tx_avg"),
                func.max(BandwidthSample.rx_bps).label("rx_max"),
                func.max(BandwidthSample.tx_bps).label("tx_max"),
                func.count().label("sample_count"),
            )
            .filter(
                BandwidthSample.sample_at >= minute_start - timedelta(minutes=1),
                BandwidthSample.sample_at < minute_start,
            )
            .group_by(BandwidthSample.subscription_id, BandwidthSample.device_id)
            .all()
        )

        if not aggregates:
            return {"pushed": 0}

        # Push to VictoriaMetrics
        async def push_aggregates():
            metrics_store = get_metrics_store()
            for agg in aggregates:
                await metrics_store.write_aggregates(
                    subscription_id=str(agg.subscription_id),
                    nas_device_id=str(agg.device_id) if agg.device_id else None,
                    timestamp=minute_start - timedelta(minutes=1),
                    rx_avg=float(agg.rx_avg or 0),
                    tx_avg=float(agg.tx_avg or 0),
                    rx_max=float(agg.rx_max or 0),
                    tx_max=float(agg.tx_max or 0),
                    sample_count=int(agg.sample_count),
                )
            await metrics_store.close()

        asyncio.run(push_aggregates())

        logger.info(f"Pushed {len(aggregates)} aggregates to VictoriaMetrics")
        return {"pushed": len(aggregates)}

    except Exception as e:
        logger.error(f"Error aggregating to metrics: {e}")
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.bandwidth.trim_redis_stream")
def trim_redis_stream():
    """
    Trim the Redis stream to prevent unbounded growth.

    Keeps only the configured max number of messages in the stream.
    """
    r = _get_redis_client()
    db = SessionLocal()

    try:
        # Get configurable max length
        max_length = _get_redis_stream_max_length(db)
        # Trim stream to max length
        trimmed = r.xtrim(REDIS_STREAM, maxlen=max_length, approximate=True)
        logger.info(f"Trimmed {trimmed} entries from bandwidth stream")
        return {"trimmed": trimmed}

    except Exception as e:
        logger.error(f"Error trimming Redis stream: {e}")
        raise
    finally:
        db.close()
        r.close()


# Helper function for bulk insert from external sources
def bulk_insert_samples(
    db,
    samples: list[dict[str, Any]],
) -> int:
    """
    Bulk insert bandwidth samples into PostgreSQL.

    Args:
        db: Database session
        samples: List of dicts with subscription_id, device_id, rx_bps, tx_bps, sample_at

    Returns:
        Number of samples inserted
    """
    if not samples:
        return 0

    objects = []
    for s in samples:
        objects.append(BandwidthSample(
            subscription_id=UUID(s["subscription_id"]) if isinstance(s["subscription_id"], str) else s["subscription_id"],
            device_id=UUID(s["device_id"]) if s.get("device_id") and isinstance(s["device_id"], str) else s.get("device_id"),
            rx_bps=int(s["rx_bps"]),
            tx_bps=int(s["tx_bps"]),
            sample_at=s["sample_at"],
        ))

    db.bulk_save_objects(objects)
    db.commit()
    return len(objects)
