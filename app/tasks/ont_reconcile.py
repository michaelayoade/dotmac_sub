"""Bounded scheduled Huawei ONT desired/observed reconciliation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 0x6F_6E_74  # "ont"


def _close_expired_remote_access() -> dict[str, int]:
    from sqlalchemy import func, select

    from app.models.network import DeviceStatus, OLTDevice, OntUnit
    from app.services.network.ont_desired_config import desired_config
    from app.services.network.ont_features import OntFeatureService

    stats = {"checked": 0, "closed": 0, "failed": 0}
    with db_session_adapter.session() as db:
        onts = list(
            db.scalars(
                select(OntUnit)
                .join(OLTDevice, OLTDevice.id == OntUnit.olt_device_id)
                .where(OntUnit.is_active.is_(True))
                .where(OntUnit.uisp_device_id.is_(None))
                .where(OLTDevice.uisp_device_id.is_(None))
                .where(OLTDevice.is_active.is_(True))
                .where(OLTDevice.status == DeviceStatus.active)
                .where(func.lower(OLTDevice.vendor) == "huawei")
            )
        )
        now = datetime.now(UTC)
        for ont in onts:
            access = desired_config(ont).get("access") or {}
            expires_raw = access.get("wan_remote_expires_at")
            if not access.get("wan_remote") or not expires_raw:
                continue
            stats["checked"] += 1
            try:
                expires_at = datetime.fromisoformat(str(expires_raw))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
            except ValueError:
                expires_at = now
            if expires_at > now:
                continue
            result = OntFeatureService.toggle_wan_remote_access(
                db, str(ont.id), enabled=False
            )
            if result.success:
                stats["closed"] += 1
            else:
                stats["failed"] += 1
                logger.warning(
                    "expired_ont_remote_access_close_failed ont=%s message=%s",
                    ont.id,
                    result.message,
                )
    return stats


@celery_app.task(
    name="app.tasks.ont_reconcile.run_ont_reconcile_sweep",
    soft_time_limit=840,
    time_limit=900,
)
def run_ont_reconcile_sweep(max_onts: int = 25) -> dict[str, Any]:
    """Reconcile the least-recently checked active ONTs without overlap."""
    from app.services.network.reconcile.sweeper import run_sweep_once

    bounded = max(1, min(int(max_onts), 100))
    with postgres_session_advisory_lock(_ADVISORY_LOCK_KEY) as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        stats = run_sweep_once(
            db_session_adapter.create_session,
            timeout_sec=45,
            max_onts=bounded,
        )
        remote_access = _close_expired_remote_access()
        return {
            "total_onts": stats.total_onts,
            "reconciled": stats.reconciled,
            "succeeded": stats.succeeded,
            "failed": stats.failed,
            "skipped_unreachable": stats.skipped_unreachable,
            "errors": stats.errors,
            "duration_sec": stats.duration_sec,
            "remote_access": remote_access,
        }
