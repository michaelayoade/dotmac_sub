"""Scheduled native Huawei ONT status polling with bounded retries."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import func, select

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)


def _olt_lock_key(olt_id: str) -> int:
    digest = hashlib.blake2b(olt_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@celery_app.task(name="app.tasks.ont_runtime_status.dispatch_huawei_ont_status")
def dispatch_huawei_ont_status() -> dict[str, int]:
    """Queue one independently retryable bulk status read per active Huawei OLT."""
    from app.models.network import DeviceStatus, OLTDevice

    with db_session_adapter.session() as db:
        olt_ids = list(
            db.scalars(
                select(OLTDevice.id).where(
                    OLTDevice.is_active.is_(True),
                    OLTDevice.status == DeviceStatus.active,
                    OLTDevice.uisp_device_id.is_(None),
                    func.lower(OLTDevice.vendor) == "huawei",
                )
            ).all()
        )
    for olt_id in olt_ids:
        refresh_huawei_olt_status.delay(str(olt_id))
    return {"queued": len(olt_ids)}


@celery_app.task(
    name="app.tasks.ont_runtime_status.refresh_huawei_olt_status",
    autoretry_for=(RuntimeError, OSError, TimeoutError),
    retry_backoff=30,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
    soft_time_limit=240,
    time_limit=300,
)
def refresh_huawei_olt_status(olt_id: str) -> dict[str, int | str]:
    """Persist one bulk OLT observation; transport/parser failures retry."""
    from app.models.network import OLTDevice
    from app.services.network.ont_runtime_status import (
        record_olt_poll_failure,
        refresh_huawei_olt_status,
    )

    with postgres_session_advisory_lock(_olt_lock_key(olt_id)) as acquired:
        if not acquired:
            return {"olt_id": olt_id, "skipped": "already_running"}
        with db_session_adapter.session() as db:
            olt = db.get(OLTDevice, olt_id)
            if olt is None or not olt.is_active:
                return {"olt_id": olt_id, "skipped": "inactive_or_missing"}
            try:
                stats = refresh_huawei_olt_status(db, olt)
            except (RuntimeError, OSError, TimeoutError) as exc:
                record_olt_poll_failure(olt, exc)
                db.commit()
                raise
            db.commit()
            return {
                "olt_id": stats.olt_id,
                "observed": stats.observed,
                "online": stats.online,
                "offline": stats.offline,
                "unmatched": stats.unmatched,
                "invalid": stats.invalid,
            }


@celery_app.task(name="app.tasks.ont_runtime_status.refresh_single_ont_status")
def refresh_single_ont_status(ont_id: str, operation_id: str) -> dict[str, object]:
    """Run a user-requested OLT/TR-069 refresh and update its durable operation."""
    from app.services.network.ont_actions import OntActions
    from app.services.network_operations import network_operations

    with db_session_adapter.session() as db:
        try:
            network_operations.mark_running(db, operation_id)
            db.commit()

            result = OntActions.refresh_status(db, ont_id)
            payload = {
                "message": result.message,
                "result": result.data or {},
            }
            if result.success:
                network_operations.mark_succeeded(
                    db, operation_id, output_payload=payload
                )
            else:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    result.message,
                    output_payload=payload,
                )
            db.commit()
            return {
                "ont_id": ont_id,
                "operation_id": operation_id,
                "success": result.success,
                **payload,
            }
        except Exception as exc:
            db.rollback()
            try:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    str(exc),
                    output_payload={"message": str(exc)},
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to record ONT refresh operation failure for %s",
                    operation_id,
                )
            logger.exception("Queued ONT status refresh failed for %s", ont_id)
            return {
                "ont_id": ont_id,
                "operation_id": operation_id,
                "success": False,
                "message": str(exc),
                "result": {},
            }
