"""Scheduled native infrastructure poll (Zabbix runtime cutover, Phase 1).

Runs the ping/SNMP reachability sweep from ``services.infrastructure_polling``
on the ingestion queue. Single-flight via ``db_session_adapter.advisory_lock``
(same helper as the topology sweeps): the sweep fans out to a thread pool with
per-device sessions, so an overlapping beat fire would double-probe every
device. The staleness windows are settings-driven so operators can tune probe
rates without a deploy.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_MS = 30_000


def _interval_setting(db, key: str, default: int, floor: int) -> int:
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_value

    try:
        value = int(resolve_value(db, SettingDomain.network_monitoring, key) or default)
    except (TypeError, ValueError):
        value = default
    return max(floor, value)


@celery_app.task(
    name="app.tasks.infrastructure_polling.run_infrastructure_poll",
    soft_time_limit=540,
    time_limit=600,
)
def run_infrastructure_poll() -> dict[str, Any]:
    """Run one native ping/SNMP sweep over active network devices."""
    from app.services.infrastructure_polling import (
        ADVISORY_LOCK_KEY,
        DEFAULT_PING_INTERVAL_SECONDS,
        DEFAULT_SNMP_INTERVAL_SECONDS,
        poll_infrastructure,
        record_poll_skip,
        record_poll_success,
    )

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            streak = record_poll_skip()
            logger.info(
                "infrastructure_poll_skipped: previous run still in progress "
                "(streak=%d)",
                streak,
            )
            return {"skipped": "already_running", "skip_streak": streak}
        try:
            ping_interval = _interval_setting(
                db,
                "infrastructure_ping_interval_seconds",
                DEFAULT_PING_INTERVAL_SECONDS,
                floor=10,
            )
            snmp_interval = _interval_setting(
                db,
                "infrastructure_snmp_interval_seconds",
                DEFAULT_SNMP_INTERVAL_SECONDS,
                floor=30,
            )
            result = poll_infrastructure(
                db,
                ping_interval_seconds=ping_interval,
                snmp_interval_seconds=snmp_interval,
            )
            db.commit()
            # Stamp the heartbeat only after a committed sweep so a stalled or
            # failing poller ages out and trips the admin alert (dead-man
            # switch — see admin_alerts poll-health findings).
            record_poll_success(result)
            return result
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("infrastructure_poll_timed_out")
            return {"error": "infrastructure_poll_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("infrastructure_poll_failed")
            return {"error": str(exc)}
