"""Asynchronous UISP write with mandatory device readback."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, nullsfirst, or_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.network_operation import NetworkOperationStatus
from app.models.uisp_control import (
    UispConfigSnapshot,
    UispDeviceIntent,
    UispIntentStatus,
    UispSnapshotSource,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operations import network_operations
from app.services.uisp import UispClient, UispClientError
from app.services.uisp_control_plane import redact_config
from app.services.uisp_write_adapter import (
    UispConfigurationWriteAdapter,
    UispPostWriteReadbackError,
    UispWriteAdapterError,
    UispWriteUnsupported,
)

logger = logging.getLogger(__name__)


def _mark_pending_readback(
    db: Session,
    operation_id: str,
    intent_id: str,
    message: str,
) -> None:
    """Persist a durable recovery marker after a possibly-applied write."""
    db.rollback()
    recovery_db = db_session_adapter.create_session()
    try:
        intent = recovery_db.get(UispDeviceIntent, intent_id)
        if intent is not None:
            intent.status = UispIntentStatus.pending_readback
            intent.last_error = message
        operation = network_operations.get(recovery_db, operation_id)
        if operation.status in {
            NetworkOperationStatus.pending,
            NetworkOperationStatus.running,
            NetworkOperationStatus.waiting,
        }:
            network_operations.mark_warning(
                recovery_db,
                operation_id,
                message,
                output_payload={
                    "outcome": "pending_readback",
                    "verified": False,
                    "write_may_have_applied": True,
                },
            )
        recovery_db.commit()
    except Exception:
        recovery_db.rollback()
        raise
    finally:
        recovery_db.close()


def execute_uisp_apply(
    operation_id: str,
    intent_id: str,
    *,
    client: UispClient | None = None,
    adapter: UispConfigurationWriteAdapter | None = None,
) -> dict[str, Any]:
    with db_session_adapter.session() as db:
        operation = network_operations.get(db, operation_id)
        intent = db.get(UispDeviceIntent, intent_id)
        if intent is None:
            network_operations.mark_failed(db, operation_id, "UISP intent not found")
            return {"success": False, "error": "intent_not_found"}
        network_operations.mark_running(db, operation_id)
        intent.status = UispIntentStatus.applying
        intent.last_error = None
        db.commit()

        result = None
        try:
            resolved_adapter = adapter or UispConfigurationWriteAdapter(
                client or UispClient.from_env()
            )
            result = resolved_adapter.apply(db, intent)
            payload = result.to_dict()
            observed_at = datetime.now(UTC)
            intent.observed_config = payload["observed_config"]
            intent.drift = payload["drift"]
            intent.last_observed_at = observed_at
            db.add(
                UispConfigSnapshot(
                    intent=intent,
                    source=UispSnapshotSource.observed,
                    revision=intent.desired_revision,
                    config=redact_config(payload["observed_config"]),
                    redacted=True,
                )
            )
            if result.verified:
                intent.status = UispIntentStatus.verified
                intent.verified_revision = intent.desired_revision
                intent.last_verified_at = observed_at
                intent.last_error = None
                network_operations.mark_succeeded(
                    db, operation_id, output_payload=payload
                )
                # Snapshot, verified intent revision, and operation success must
                # become durable atomically. A later context-manager commit is
                # too late to recover cleanly from an audit persistence failure.
                db.commit()
                return {"success": True, **payload}
            intent.status = UispIntentStatus.drifted
            intent.last_error = result.message
            network_operations.mark_failed(
                db, operation_id, result.message, output_payload=payload
            )
            db.commit()
            return {"success": False, **payload}
        except UispPostWriteReadbackError as exc:
            message = str(exc)
            _mark_pending_readback(db, operation_id, intent_id, message)
            return {
                "success": False,
                "outcome": "pending_readback",
                "verified": False,
                "message": message,
            }
        except UispWriteUnsupported as exc:
            message = str(exc)
            intent.status = UispIntentStatus.manual_required
            intent.last_error = message
            payload = {"outcome": "unsupported", "verified": False, "message": message}
            network_operations.mark_warning(
                db, operation_id, message, output_payload=payload
            )
            return {"success": False, **payload}
        except (UispWriteAdapterError, UispClientError) as exc:
            message = str(exc)
            intent.status = UispIntentStatus.failed
            intent.last_error = message
            payload = {"outcome": "failed", "verified": False, "message": message}
            network_operations.mark_failed(
                db, operation_id, message, output_payload=payload
            )
            return {"success": False, **payload}
        except Exception as exc:  # noqa: BLE001 - terminal operation audit
            logger.exception(
                "uisp_apply_failed operation=%s intent=%s", operation.id, intent.id
            )
            message = f"Unexpected UISP apply failure: {exc}"
            if result is not None and result.write_accepted:
                recovery_message = (
                    "UISP write/readback completed but atomic audit persistence "
                    f"failed: {exc}"
                )
                _mark_pending_readback(db, operation_id, intent_id, recovery_message)
                return {
                    "success": False,
                    "outcome": "pending_readback",
                    "verified": False,
                    "message": recovery_message,
                }
            db.rollback()
            intent = db.get(UispDeviceIntent, intent_id)
            if intent is None:
                return {"success": False, "outcome": "failed", "message": message}
            intent.status = UispIntentStatus.failed
            intent.last_error = message
            network_operations.mark_failed(db, operation_id, message)
            db.commit()
            return {"success": False, "outcome": "failed", "message": message}


@celery_app.task(
    name="app.tasks.uisp_control.apply_uisp_intent",
    soft_time_limit=90,
    time_limit=120,
)
def apply_uisp_intent(operation_id: str, intent_id: str) -> dict[str, Any]:
    return execute_uisp_apply(operation_id, intent_id)


@celery_app.task(
    name="app.tasks.uisp_control.reconcile_uisp_config_readback",
    soft_time_limit=240,
    time_limit=300,
)
def reconcile_uisp_config_readback(max_intents: int = 25) -> dict[str, Any]:
    from app.services.uisp import uisp_configured

    if not uisp_configured():
        return {"skipped": "uisp_token_missing"}
    bounded = max(1, min(int(max_intents), 100))
    stats = {"checked": 0, "verified": 0, "drifted": 0, "unsupported": 0, "failed": 0}
    with db_session_adapter.session() as db:
        stale_applying_before = datetime.now(UTC) - timedelta(minutes=5)
        intents = (
            db.query(UispDeviceIntent)
            .filter(
                or_(
                    UispDeviceIntent.status.in_(
                        {
                            UispIntentStatus.verified,
                            UispIntentStatus.drifted,
                            UispIntentStatus.manual_required,
                            UispIntentStatus.pending_readback,
                        }
                    ),
                    and_(
                        UispDeviceIntent.status == UispIntentStatus.applying,
                        UispDeviceIntent.updated_at < stale_applying_before,
                    ),
                )
            )
            .order_by(nullsfirst(UispDeviceIntent.last_observed_at.asc()))
            .limit(bounded)
            .all()
        )
        adapter = UispConfigurationWriteAdapter(
            UispClient.from_env(), readback_attempts=1, readback_delay_seconds=0
        )
        for intent in intents:
            stats["checked"] += 1
            try:
                result = adapter.readback(db, intent)
                payload = result.to_dict()
                now = datetime.now(UTC)
                if intent.observed_config != payload["observed_config"]:
                    db.add(
                        UispConfigSnapshot(
                            intent=intent,
                            source=UispSnapshotSource.observed,
                            revision=intent.desired_revision,
                            config=payload["observed_config"],
                            redacted=True,
                        )
                    )
                intent.observed_config = payload["observed_config"]
                intent.drift = payload["drift"]
                intent.last_observed_at = now
                if result.verified:
                    intent.status = UispIntentStatus.verified
                    intent.verified_revision = intent.desired_revision
                    intent.last_verified_at = now
                    intent.last_error = None
                    stats["verified"] += 1
                else:
                    intent.status = UispIntentStatus.drifted
                    intent.last_error = result.message
                    stats["drifted"] += 1
            except UispWriteUnsupported as exc:
                intent.status = UispIntentStatus.manual_required
                intent.last_error = str(exc)
                stats["unsupported"] += 1
            except (UispWriteAdapterError, UispClientError) as exc:
                intent.last_error = str(exc)
                stats["failed"] += 1
            db.flush()
    return stats
