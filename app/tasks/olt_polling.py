"""Celery tasks for OLT optical signal polling."""

from __future__ import annotations

import logging

from sqlalchemy import select, text

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


def _olt_lock_key(olt_id: str) -> int:
    """Generate a unique advisory lock key for an OLT device.

    P2 FIX: Use larger hash range to prevent collisions.
    PostgreSQL advisory locks accept bigint (-2^63 to 2^63-1).
    We use a 32-bit hash within a dedicated namespace to avoid collisions
    with other advisory lock users while maintaining uniqueness.
    """
    import hashlib

    # Use SHA256 for deterministic hashing (Python's hash() varies between runs)
    # Take first 8 bytes as a signed 64-bit integer
    hash_bytes = hashlib.sha256(olt_id.encode()).digest()[:8]
    hash_int = int.from_bytes(hash_bytes, byteorder="big", signed=True)

    # Use a namespace prefix (7042) shifted to high bits, combined with hash
    # This gives us ~2^60 unique keys in a dedicated namespace
    namespace = 7042 << 48
    return namespace | (hash_int & 0x0000FFFFFFFFFFFF)


@celery_app.task(name="app.tasks.olt_polling.poll_single_olt")
def poll_single_olt(olt_id: str) -> dict[str, int | str]:
    """Poll a single OLT device for ONT signal levels and health.

    This task is designed to run in parallel with other OLT polling tasks.
    Each task handles its own database session and transaction.
    Uses per-OLT advisory lock to prevent concurrent polling of the same device.

    Args:
        olt_id: UUID string of the OLT to poll.

    Returns:
        Stats dict with olt_name, polled, updated, errors.
    """
    logger.info("Starting single OLT poll for %s", olt_id)
    db = SessionLocal()
    lock_key = _olt_lock_key(olt_id)
    lock_acquired = False
    try:
        # Per-OLT advisory lock to prevent concurrent polling of same device
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": lock_key},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning(
                "Skipping OLT %s poll: another poll already in progress", olt_id
            )
            return {
                "olt_id": olt_id,
                "polled": 0,
                "updated": 0,
                "errors": 0,
                "skipped_due_to_lock": 1,
            }

        from app.services.network.olt_polling import poll_single_olt_device

        result = poll_single_olt_device(db, olt_id)
        logger.info(
            "Single OLT poll complete for %s: %d polled, %d updated, %d errors",
            result.get("olt_name", olt_id),
            result.get("polled", 0),
            result.get("updated", 0),
            result.get("errors", 0),
        )
        return result
    except Exception as e:
        logger.error("Single OLT poll failed for %s: %s", olt_id, e, exc_info=True)
        db.rollback()
        return {"olt_id": olt_id, "polled": 0, "updated": 0, "errors": 1, "error": str(e)}
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
            except Exception:
                logger.exception("Failed to release OLT %s polling lock", olt_id)
        db.close()


@celery_app.task(name="app.tasks.olt_polling.poll_all_olt_signals")
def poll_all_olt_signals() -> dict[str, int]:
    """Periodic task to poll all active OLTs for ONT signal levels.

    First marks stale ONTs as offline (those not seen in 2x poll interval),
    then fans out to parallel poll_single_olt tasks for each active OLT.
    Each subtask runs independently with its own per-OLT advisory lock,
    preventing concurrent polling of the same device even if this
    orchestrator is triggered multiple times.

    Returns:
        Statistics dict with olts_dispatched and stale_marked_offline counts.
    """
    logger.info("Starting parallel OLT signal polling orchestrator")
    db = SessionLocal()
    stale_marked = 0
    try:
        # Mark stale ONTs as offline BEFORE dispatching new polls
        # This eliminates race conditions with finalize_olt_polling
        # ONTs not updated in 10 minutes (2x poll interval) are considered stale
        try:
            stale_marked = _mark_stale_onts_offline(db, stale_threshold_minutes=10)
            if stale_marked > 0:
                logger.info("Marked %d stale ONTs as offline", stale_marked)
        except Exception as exc:
            logger.warning("Failed to mark stale ONTs offline: %s", exc)

        # Get all active OLTs
        stmt = select(OLTDevice).where(OLTDevice.is_active.is_(True))
        olts = list(db.scalars(stmt).all())

        if not olts:
            logger.info("No active OLTs found for signal polling")
            return {"olts_dispatched": 0, "stale_marked_offline": stale_marked}

        logger.info("Dispatching parallel polls for %d OLTs", len(olts))

        # Fan out to parallel tasks (fire-and-forget)
        # Each subtask handles its own DB session, per-OLT lock, and commits independently
        dispatched = 0
        for olt in olts:
            from app.celery_app import enqueue_celery_task

            enqueue_celery_task(
                poll_single_olt,
                args=[str(olt.id)],
                correlation_id=f"olt_poll:{olt.id}",
                source="poll_all_olts",
            )
            dispatched += 1
            logger.debug("Dispatched poll task for OLT %s (%s)", olt.name, olt.id)

        logger.info(
            "Parallel OLT signal polling orchestrator complete: dispatched %d tasks",
            dispatched,
        )

        return {"olts_dispatched": dispatched, "stale_marked_offline": stale_marked}
    except Exception as e:
        logger.error("OLT signal polling orchestrator failed: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


def _mark_stale_onts_offline(db, stale_threshold_minutes: int = 10) -> int:
    """Mark ONTs as offline if they haven't been polled recently.

    P2 FIX: Only marks ONTs offline if their parent OLT was successfully
    polled recently. This prevents false-positives during OLT downtime
    or network issues where the OLT itself was unreachable.

    ONTs that are currently 'online' but haven't had their signal_updated_at
    refreshed within the threshold are marked offline with reason 'los',
    but only if the OLT's last_poll_at is recent (indicating the OLT
    was reachable but the ONT wasn't seen).

    Args:
        db: Database session.
        stale_threshold_minutes: Minutes without update before marking offline.

    Returns:
        Number of ONTs marked offline.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models.network import (
        OLTDevice,
        OntUnit,
        OnuOfflineReason,
        OnuOnlineStatus,
        OntStatusSource,
        PollStatus,
    )

    now = datetime.now(UTC)
    threshold = now - timedelta(minutes=stale_threshold_minutes)

    # P2 FIX: Get OLTs that were SUCCESSFULLY polled recently
    # Only mark ONTs offline if their OLT was reachable AND poll succeeded
    # This prevents false positives when OLT poll fails (timeout, network issue)
    olt_poll_threshold = now - timedelta(minutes=stale_threshold_minutes * 2)
    reachable_olt_ids = [
        olt.id
        for olt in db.scalars(
            select(OLTDevice).where(
                OLTDevice.is_active.is_(True),
                OLTDevice.last_poll_at.isnot(None),
                OLTDevice.last_poll_at >= olt_poll_threshold,
                OLTDevice.last_poll_status == PollStatus.success,
            )
        ).all()
    ]

    if not reachable_olt_ids:
        logger.info("No recently-polled OLTs found; skipping stale ONT marking")
        return 0

    # Find stale ONTs: online status but not seen recently
    # AND their OLT was recently polled (so the ONT should have been seen)
    result = db.execute(
        update(OntUnit)
        .where(OntUnit.online_status == OnuOnlineStatus.online)
        .where(OntUnit.is_active.is_(True))
        .where(OntUnit.olt_device_id.in_(reachable_olt_ids))
        .where(
            (OntUnit.signal_updated_at < threshold)
            | (OntUnit.signal_updated_at.is_(None))
        )
        .values(
            online_status=OnuOnlineStatus.offline,
            offline_reason=OnuOfflineReason.los,
            effective_status=OnuOnlineStatus.offline,
            effective_status_source=OntStatusSource.olt,
            status_resolved_at=now,
        )
    )
    db.commit()

    marked = result.rowcount
    if marked > 0:
        logger.info(
            "Marked %d stale ONTs offline (OLTs polled but ONTs not seen in %d min)",
            marked,
            stale_threshold_minutes,
        )
    return marked


@celery_app.task(name="app.tasks.olt_polling.finalize_olt_polling")
def finalize_olt_polling() -> dict[str, int]:
    """Push aggregated ONU/signal metrics to VictoriaMetrics.

    Called by celery beat on the same schedule as poll_all_olt_signals.
    Pushes current ONU status counts and per-ONT signal metrics.

    Note: Stale ONT detection is now handled at the START of poll_all_olt_signals
    to avoid race conditions with parallel poll tasks.
    """
    logger.info("Pushing ONU/signal metrics to VictoriaMetrics")
    db = SessionLocal()
    try:
        # Push ONU status counts to VictoriaMetrics
        try:
            from app.services.monitoring_metrics import push_onu_status_metrics
            from app.services.network_monitoring import get_onu_status_summary

            onu = get_onu_status_summary(db)
            push_onu_status_metrics(
                online=onu.get("online", 0),
                offline=onu.get("offline", 0),
                low_signal=onu.get("low_signal", 0),
            )
            logger.info("Pushed ONU status metrics: %s", onu)
        except Exception as exc:
            logger.warning("Failed to push ONU metrics to VictoriaMetrics: %s", exc)

        # Push signal metrics
        try:
            from app.services.network.olt_polling_metrics import _push_signal_metrics

            metrics_count = _push_signal_metrics(db)
            logger.info("Pushed %d signal metrics to VictoriaMetrics", metrics_count)
            return {"metrics_pushed": metrics_count}
        except Exception as e:
            logger.error("Signal metrics push failed: %s", e)
            return {"metrics_pushed": 0}
    finally:
        db.close()
