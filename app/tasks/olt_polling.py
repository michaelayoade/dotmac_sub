"""Celery tasks for OLT optical signal polling.

NOTE: OLT SNMP polling has been moved to Zabbix. These tasks are retained
for stale ONT detection and backwards compatibility, but actual polling
is now handled externally with data ingested via zabbix_data_ingest.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_polling.poll_single_olt")
def poll_single_olt(olt_id: str) -> dict[str, int | str]:
    """Poll a single OLT device for ONT signal levels and health.

    DEPRECATED: OLT SNMP polling has been moved to Zabbix.
    This task is retained for backwards compatibility but returns
    an error indicating the function has been disabled.

    Args:
        olt_id: UUID string of the OLT to poll.

    Returns:
        Error dict indicating polling is disabled.
    """
    logger.warning(
        "poll_single_olt called for %s but OLT SNMP polling is now handled by Zabbix",
        olt_id,
    )
    return {
        "olt_id": olt_id,
        "polled": 0,
        "updated": 0,
        "errors": 1,
        "error": "OLT SNMP polling disabled - now handled by Zabbix",
    }


@celery_app.task(name="app.tasks.olt_polling.poll_all_olt_signals")
def poll_all_olt_signals() -> dict[str, int]:
    """Periodic task to poll all active OLTs for ONT signal levels.

    DEPRECATED: OLT SNMP polling has been moved to Zabbix.
    This task now only handles stale ONT detection; actual polling
    is performed by Zabbix with data ingested via zabbix_data_ingest.

    Returns:
        Statistics dict with olts_dispatched (always 0) and stale_marked_offline counts.
    """
    logger.info("Running stale ONT detection (SNMP polling now handled by Zabbix)")
    stale_marked = 0
    try:
        with db_session_adapter.session() as db:
            # Mark stale ONTs as unknown
            try:
                stale_marked = _mark_stale_onts_offline(db, stale_threshold_minutes=10)
                if stale_marked > 0:
                    logger.info("Marked %d stale ONTs as unknown", stale_marked)
            except Exception as exc:
                logger.warning("Failed to mark stale ONTs unknown: %s", exc)

        return {"olts_dispatched": 0, "stale_marked_offline": stale_marked}
    except Exception as e:
        logger.error("Stale ONT detection failed: %s", e, exc_info=True)
        raise


def _mark_stale_onts_offline(db, stale_threshold_minutes: int = 10) -> int:
    """Mark stale ONTs as unknown if they haven't been polled recently.

    Missing/stale poll data is not proof that the ONT is offline.  This task
    only downgrades stale OLT-side status to unknown.  Explicit fresh OLT
    readings are responsible for setting offline.

    Args:
        db: Database session.
        stale_threshold_minutes: Minutes without update before marking unknown.

    Returns:
        Number of ONTs marked unknown because the OLT poll did not refresh them.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func
    from sqlalchemy.orm import joinedload

    from app.models.network import (
        OLTDevice,
        OntUnit,
        OnuOnlineStatus,
        PollStatus,
    )
    from app.services.network.ont_status import resolve_ont_status_for_model

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

    stale_filter = (OntUnit.signal_updated_at < threshold) | (
        OntUnit.signal_updated_at.is_(None)
    )

    huawei_olt_ids = [
        olt_id
        for olt_id in db.scalars(
            select(OLTDevice.id).where(
                OLTDevice.id.in_(reachable_olt_ids),
                func.lower(func.coalesce(OLTDevice.vendor, "")).like("%huawei%"),
            )
        ).all()
    ]
    huawei_packed_external_id = func.lower(func.coalesce(OntUnit.external_id, "")).like(
        "huawei:%.%"
    )
    huawei_non_deterministic_identity = (
        OntUnit.olt_device_id.in_(huawei_olt_ids) & ~huawei_packed_external_id
    )

    unknown_candidates = list(
        db.scalars(
            select(OntUnit)
            .options(
                joinedload(OntUnit.tr069_acs_server),
                joinedload(OntUnit.olt_device).joinedload(OLTDevice.tr069_acs_server),
            )
            .where(OntUnit.online_status == OnuOnlineStatus.online)
            .where(OntUnit.is_active.is_(True))
            .where(huawei_non_deterministic_identity)
            .where(stale_filter)
        ).all()
    )
    unknown_marked = 0
    for ont in unknown_candidates:
        ont.online_status = OnuOnlineStatus.unknown
        ont.offline_reason = None
        snapshot = resolve_ont_status_for_model(ont, now=now)
        ont.acs_status = snapshot.acs_status
        ont.acs_last_inform_at = snapshot.acs_last_inform_at
        ont.effective_status = snapshot.effective_status
        ont.effective_status_source = snapshot.effective_status_source
        ont.status_resolved_at = snapshot.status_resolved_at
        unknown_marked += 1

    # Find stale ONTs: online status but not seen recently
    # AND their OLT was recently polled (so the ONT should have been seen).
    # Huawei rows without packed SNMP identity are deliberately excluded above:
    # when polling cannot map the packed index, the safe state is unknown, not LOS.
    stale_candidates = list(
        db.scalars(
            select(OntUnit)
            .options(
                joinedload(OntUnit.tr069_acs_server),
                joinedload(OntUnit.olt_device).joinedload(OLTDevice.tr069_acs_server),
            )
            .where(OntUnit.online_status == OnuOnlineStatus.online)
            .where(OntUnit.is_active.is_(True))
            .where(OntUnit.olt_device_id.in_(reachable_olt_ids))
            .where(~huawei_non_deterministic_identity)
            .where(stale_filter)
        ).all()
    )
    marked = 0
    for ont in stale_candidates:
        ont.online_status = OnuOnlineStatus.unknown
        ont.offline_reason = None
        snapshot = resolve_ont_status_for_model(ont, now=now)
        ont.acs_status = snapshot.acs_status
        ont.acs_last_inform_at = snapshot.acs_last_inform_at
        ont.effective_status = snapshot.effective_status
        ont.effective_status_source = snapshot.effective_status_source
        ont.status_resolved_at = snapshot.status_resolved_at
        marked += 1

    db.commit()

    if unknown_marked > 0:
        logger.info(
            "Marked %d stale Huawei ONTs with non-packed SNMP identity as unknown",
            unknown_marked,
        )
    if marked > 0:
        logger.info(
            "Marked %d stale ONTs unknown (OLTs polled but ONTs not seen in %d min)",
            marked,
            stale_threshold_minutes,
        )
    return marked + unknown_marked


@celery_app.task(name="app.tasks.olt_polling.finalize_olt_polling")
def finalize_olt_polling() -> dict[str, int]:
    """Push aggregated ONU/signal metrics to VictoriaMetrics.

    Called by celery beat on the same schedule as poll_all_olt_signals.
    Pushes current ONU status counts and per-ONT signal metrics.

    Note: Stale ONT detection is now handled at the START of poll_all_olt_signals
    to avoid race conditions with parallel poll tasks.
    """
    logger.info("Pushing ONU/signal metrics to VictoriaMetrics")
    with db_session_adapter.read_session() as db:
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
