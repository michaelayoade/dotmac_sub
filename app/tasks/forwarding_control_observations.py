"""Scheduled read-only RouterOS forwarding-observation collection."""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.services import control_registry, settings_spec
from app.services.db_session_adapter import db_session_adapter
from app.services.network.forwarding_observation_collector import (
    DEFAULT_OBSERVATION_TTL_SECONDS,
    collect_forwarding_control_observations,
)
from app.services.topology.coverage_metrics import store_task_stats

logger = logging.getLogger(__name__)

CONTROL_KEY = "network.forwarding_observation_collection"
DEFAULT_INTERVAL_SECONDS = 300


def _seconds(db: Any, key: str, default: int) -> int:
    value = settings_spec.resolve_value(
        db,
        SettingDomain.network_monitoring,
        key,
    )
    try:
        return int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


@celery_app.task(
    name="app.tasks.forwarding_control_observations.run_forwarding_control_observation_poll",
    soft_time_limit=300,
    time_limit=360,
)
def run_forwarding_control_observation_poll() -> dict[str, Any]:
    """Collect expiring facts only when the fail-closed control is enabled."""

    db = db_session_adapter.create_session()
    try:
        if not control_registry.is_enabled(db, CONTROL_KEY):
            result: dict[str, Any] = {
                "control": CONTROL_KEY,
                "status": "disabled",
            }
        else:
            interval_seconds = max(
                _seconds(
                    db,
                    "forwarding_control_observation_interval_seconds",
                    DEFAULT_INTERVAL_SECONDS,
                ),
                60,
            )
            ttl_seconds = max(
                _seconds(
                    db,
                    "forwarding_control_observation_ttl_seconds",
                    DEFAULT_OBSERVATION_TTL_SECONDS,
                ),
                interval_seconds * 2,
            )
            result = collect_forwarding_control_observations(
                db,
                ttl_seconds=ttl_seconds,
            )
            result["control"] = CONTROL_KEY
            result["status"] = "collected"
            db.commit()
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("forwarding_control_observation_poll_timed_out")
        result = {"error": "forwarding_control_observation_poll_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("forwarding_control_observation_poll_failed")
        result = {"error": str(exc)}
    finally:
        db.close()
    store_task_stats("forwarding_control_observation_poll", result)
    return result


__all__ = ["run_forwarding_control_observation_poll"]
