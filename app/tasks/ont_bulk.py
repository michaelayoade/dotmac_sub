"""Bulk ONT operations executed as Celery tasks."""

from __future__ import annotations

import logging
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_bulk.execute_bulk_action")
def execute_bulk_action(
    ont_ids: list[str], action: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Execute an action on multiple ONTs.

    Args:
        ont_ids: List of OntUnit UUIDs.
        action: Action name (reboot, factory_reset, speed_update, etc.).
        params: Additional parameters for the action.

    Returns:
        Statistics dict with processed/errors/skipped counts.
    """
    if action == "provision_saga":
        return _queue_bulk_provision_saga(ont_ids, params)

    logger.info("Starting bulk %s for %d ONT(s)", action, len(ont_ids))
    processed = 0
    errors = 0
    skipped = 0

    with db_session_adapter.session() as db:
        for ont_id in ont_ids:
            try:
                result = _dispatch_action(db, ont_id, action, params)
                if result is None:
                    skipped += 1
                elif result.success:
                    processed += 1
                else:
                    logger.warning(
                        "Bulk %s failed for ONT %s: %s",
                        action,
                        ont_id,
                        result.message,
                    )
                    errors += 1
            except Exception as exc:
                logger.error("Bulk %s error for ONT %s: %s", action, ont_id, exc)
                errors += 1

    stats = {"processed": processed, "errors": errors, "skipped": skipped}
    logger.info("Bulk %s complete: %s", action, stats)
    return stats


def _dispatch_action(db, ont_id: str, action: str, params: dict):  # type: ignore[no-untyped-def]
    """Route a bulk action to the appropriate service method."""
    from app.services.network.ont_action_common import ActionResult

    if action == "reboot":
        from app.services.network.ont_actions import ont_actions

        return ont_actions.reboot(db, ont_id)

    if action == "factory_reset":
        from app.services.network.ont_actions import ont_actions

        return ont_actions.factory_reset(db, ont_id)

    if action == "speed_update":
        from app.services.network.ont_write import ont_write

        return ont_write.update_speed_profile(
            db,
            ont_id,
            download_profile_id=params.get("download_profile_id"),
            upload_profile_id=params.get("upload_profile_id"),
        )

    if action == "catv_toggle":
        from app.services.network.ont_features import ont_features

        return ont_features.toggle_catv(
            db, ont_id, enabled=params.get("enabled", False)
        )

    if action == "wifi_update":
        from app.services.network.ont_features import ont_features

        return ont_features.set_wifi_config(
            db,
            ont_id,
            ssid=params.get("ssid"),
            password=params.get("password"),
            enabled=params.get("enabled"),
        )

    if action == "voip_toggle":
        from app.services.network.ont_features import ont_features

        return ont_features.toggle_voip(
            db, ont_id, enabled=params.get("enabled", False)
        )

    logger.warning("Unknown bulk action: %s", action)
    return ActionResult(success=False, message=f"Unknown action: {action}")


def _queue_bulk_provision_saga(
    ont_ids: list[str],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Queue many per-ONT provisioning sagas with bounded fan-out."""
    from app.services.network.bulk_provisioning import bulk_provision_onts

    params = dict(params or {})
    saga_name = str(params.get("saga_name") or "full_provisioning")
    with db_session_adapter.session() as db:
        result = bulk_provision_onts(
            db,
            ont_ids,
            profile_id=params.get("profile_id"),
            saga_name=saga_name,
            tr069_olt_profile_id=params.get("tr069_olt_profile_id"),
            max_workers=int(params.get("max_parallel") or 10),
            chunk_delay_seconds=int(params.get("chunk_delay_seconds") or 15),
            initiated_by=params.get("initiated_by"),
            correlation_key=params.get("correlation_key"),
            dry_run=bool(params.get("dry_run", False)),
            allow_low_optical_margin=bool(
                params.get("allow_low_optical_margin", False)
            ),
            step_data=dict(params.get("step_data") or {}),
            metadata={"source": "ont_bulk_action"},
        )

    return {
        "processed": 0,
        "errors": 0,
        "skipped": result.skipped,
        "queued": result.queued,
        "bulk_run_id": str(result.run_id),
        "correlation_key": result.correlation_key,
        "orchestrator_task_id": result.orchestrator_task_id,
        "saga_name": saga_name,
    }
