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


def _reconcile_payload(result: Any) -> dict[str, Any]:
    failure = result.failure
    return {
        "success": result.success,
        "sync_status": result.sync_status,
        "duration_ms": result.duration_ms,
        "failure": (
            {"reason": failure.reason, "message": failure.message}
            if failure is not None
            else None
        ),
        # Values can contain subscriber credentials. Operation history records
        # only which fields changed, not their old/new values.
        "actions": [
            {
                "field": action.field,
                "surface": action.surface,
                "duration_ms": action.duration_ms,
            }
            for action in result.actions_applied
        ],
        "drift_before": [drift.field for drift in result.drift_before],
        "drift_after": [drift.field for drift in result.drift_after],
    }


@celery_app.task(
    name="app.tasks.ont_reconcile.reconcile_huawei_ont",
    soft_time_limit=150,
    time_limit=180,
)
def reconcile_huawei_ont(ont_id: str, operation_id: str) -> dict[str, Any]:
    """Run one tracked desired/observed reconcile and persist its outcome."""
    from app.services.network.reconcile.core import reconcile_ont
    from app.services.network_operations import network_operations

    with db_session_adapter.session() as db:
        try:
            from app.models.network import OntUnit
            from app.services import tr069 as tr069_service
            from app.services.network.acs_resolution import resolve_acs_for_ont

            operation = network_operations.mark_running(db, operation_id)
            parent_id = str(operation.parent_id) if operation.parent_id else None
            db.commit()
            ont = db.get(OntUnit, ont_id)
            desired_acs = (
                resolve_acs_for_ont(db, ont).server if ont is not None else None
            )
            proposed_change = (
                {
                    "acs_url": desired_acs.cwmp_url,
                    "acs_username": desired_acs.cwmp_username,
                    "acs_password_ref": desired_acs.cwmp_password,
                }
                if desired_acs is not None
                else None
            )
            result = reconcile_ont(
                db,
                ont_id,
                proposed_change=proposed_change,
                mode="sweep",
                timeout_sec=120,
            )
            payload = _reconcile_payload(result)
            if result.success:
                if ont is not None and desired_acs is not None:
                    tr069_service.sync_ont_acs_server(db, ont, desired_acs.id)
                network_operations.mark_succeeded(
                    db, operation_id, output_payload=payload
                )
            else:
                message = (
                    result.failure.message
                    if result.failure is not None
                    else "ONT reconciliation did not converge"
                )
                network_operations.mark_failed(
                    db, operation_id, message, output_payload=payload
                )
            if parent_id:
                network_operations.update_parent_status(db, parent_id)
            db.commit()
            return {"ont_id": ont_id, "operation_id": operation_id, **payload}
        except Exception as exc:
            db.rollback()
            try:
                operation = network_operations.get(db, operation_id)
                network_operations.mark_failed(
                    db,
                    operation_id,
                    str(exc),
                    output_payload={"success": False, "message": str(exc)},
                )
                if operation.parent_id:
                    network_operations.update_parent_status(
                        db, str(operation.parent_id)
                    )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to record Huawei ONT reconcile failure for %s",
                    operation_id,
                )
            logger.exception("Queued Huawei ONT reconcile failed for %s", ont_id)
            return {
                "ont_id": ont_id,
                "operation_id": operation_id,
                "success": False,
                "message": str(exc),
            }


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
            max_duration_sec=720,
        )
        remote_access = _close_expired_remote_access()
        return {
            "total_onts": stats.total_onts,
            "reconciled": stats.reconciled,
            "succeeded": stats.succeeded,
            "failed": stats.failed,
            "deferred": stats.deferred,
            "skipped_unreachable": stats.skipped_unreachable,
            "errors": stats.errors,
            "duration_sec": stats.duration_sec,
            "remote_access": remote_access,
        }
