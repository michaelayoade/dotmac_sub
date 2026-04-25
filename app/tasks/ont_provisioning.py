"""Celery tasks for ONT provisioning."""

import logging
import math
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.queue_adapter import enqueue_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_provisioning.provision_ont")
def provision_ont(
    ont_id: str,
    *,
    tr069_olt_profile_id: int | None = None,
    dry_run: bool = False,
    initiated_by: str | None = None,
    correlation_key: str | None = None,
    bulk_run_id: str | None = None,
    bulk_item_id: str | None = None,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
) -> dict[str, Any]:
    """Provision one ONT from OLT defaults plus OntUnit.desired_config."""
    del initiated_by  # Reserved for audit propagation when needed.
    with db_session_adapter.session() as db:
        if bulk_item_id:
            from app.services.network.bulk_provisioning import mark_bulk_item_running

            mark_bulk_item_running(db, bulk_item_id)
            db.commit()

        try:
            from app.services.network.ont_provisioning.orchestrator import (
                provision_ont_from_desired_config,
            )
            from app.services.network.provisioning_events import (
                provisioning_correlation,
            )

            effective_correlation = correlation_key or f"provision:{ont_id}"
            with provisioning_correlation(effective_correlation):
                result = provision_ont_from_desired_config(
                    db,
                    ont_id,
                    tr069_olt_profile_id=tr069_olt_profile_id,
                    dry_run=dry_run,
                    allow_low_optical_margin=allow_low_optical_margin,
                    wait_for_acs=wait_for_acs,
                    apply_acs_config=apply_acs_config,
                )
            payload = result.to_dict()
            payload.update(
                {
                    "bulk_run_id": bulk_run_id,
                    "bulk_item_id": bulk_item_id,
                    "correlation_key": effective_correlation,
                }
            )
            if bulk_item_id:
                from app.services.network.bulk_provisioning import (
                    mark_bulk_item_completed,
                )

                mark_bulk_item_completed(db, bulk_item_id, payload)
                db.commit()
            return payload
        except Exception as exc:
            logger.exception("Direct ONT provisioning task failed for %s", ont_id)
            if bulk_item_id:
                from app.services.network.bulk_provisioning import mark_bulk_item_failed

                mark_bulk_item_failed(db, bulk_item_id, str(exc))
                db.commit()
            return {
                "success": False,
                "message": f"Provisioning task error: {exc}",
                "ont_id": ont_id,
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": bulk_item_id,
                "correlation_key": correlation_key,
            }


@celery_app.task(name="app.tasks.ont_provisioning.queue_bulk_provisioning")
def queue_bulk_provisioning(
    ont_ids: list[str],
    *,
    tr069_olt_profile_id: int | None = None,
    dry_run: bool = False,
    initiated_by: str | None = None,
    max_parallel: int = 10,
    chunk_delay_seconds: int = 15,
    bulk_run_id: str | None = None,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
) -> dict[str, Any]:
    """Queue direct provisioning tasks for many ONTs with bounded fan-out."""
    bulk_items_by_ont_id: dict[str, Any] = {}
    if bulk_run_id:
        with db_session_adapter.read_session() as session:
            from app.services.network.bulk_provisioning import list_pending_bulk_items

            pending_items = list_pending_bulk_items(session, bulk_run_id)
            bulk_items_by_ont_id = {
                str(item.ont_unit_id): item for item in pending_items if item.ont_unit_id
            }

    unique_ont_ids = list(dict.fromkeys(str(ont_id) for ont_id in ont_ids if ont_id))
    if bulk_items_by_ont_id:
        unique_ont_ids = [
            ont_id for ont_id in unique_ont_ids if ont_id in bulk_items_by_ont_id
        ]
    if not unique_ont_ids:
        return {
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "message": "No ONTs supplied.",
            "tasks": [],
        }

    max_parallel = max(1, min(int(max_parallel or 10), 50))
    chunk_delay_seconds = max(0, int(chunk_delay_seconds or 0))
    total_chunks = math.ceil(len(unique_ont_ids) / max_parallel)
    tasks: list[dict[str, str | int]] = []
    dispatch_errors = 0

    for index, ont_id in enumerate(unique_ont_ids):
        chunk_index = index // max_parallel
        countdown = chunk_index * chunk_delay_seconds
        bulk_item = bulk_items_by_ont_id.get(ont_id)
        item_correlation_key = (
            getattr(bulk_item, "correlation_key", None)
            if bulk_item is not None
            else f"bulk_provision:{ont_id}"
        )
        dispatch = enqueue_task(
            "app.tasks.ont_provisioning.provision_ont",
            kwargs={
                "ont_id": ont_id,
                "tr069_olt_profile_id": tr069_olt_profile_id,
                "dry_run": dry_run,
                "initiated_by": initiated_by,
                "correlation_key": item_correlation_key,
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": str(bulk_item.id) if bulk_item is not None else None,
                "allow_low_optical_margin": allow_low_optical_margin,
                "wait_for_acs": wait_for_acs,
                "apply_acs_config": apply_acs_config,
            },
            correlation_id=item_correlation_key,
            source="bulk_provisioning_orchestrator",
            countdown=countdown,
        )
        if not dispatch.queued:
            dispatch_errors += 1
        tasks.append(
            {
                "ont_id": ont_id,
                "task_id": dispatch.task_id or "",
                "chunk": chunk_index + 1,
                "countdown": countdown,
                "bulk_item_id": str(bulk_item.id) if bulk_item is not None else "",
                "correlation_key": item_correlation_key,
                **({"error": dispatch.error or "queue_dispatch_failed"} if not dispatch.queued else {}),
            }
        )

    stats = {
        "queued": len(tasks) - dispatch_errors,
        "errors": dispatch_errors,
        "skipped": len(ont_ids) - len(unique_ont_ids),
        "max_parallel": max_parallel,
        "chunks": total_chunks,
        "bulk_run_id": bulk_run_id,
        "tasks": tasks,
    }
    logger.info("Bulk direct provisioning queued: %s", stats)
    return stats


@celery_app.task(name="app.tasks.ont_provisioning.detect_profile_drift")
def detect_profile_drift() -> dict[str, int]:
    """Legacy compatibility no-op after profile/bundle removal."""
    logger.info("Skipping obsolete ONT profile drift detection")
    return {"drifted": 0, "total_field_mismatches": 0, "errors": 0}


@celery_app.task(name="app.tasks.ont_provisioning.auto_link_profiles")
def auto_link_profiles() -> dict[str, int]:
    """Legacy compatibility task retained as a no-op after bundle cutover."""
    logger.info(
        "Skipping legacy ONT auto-link task because bundle templates are materialized onto ONT desired state"
    )
    return {"linked": 0, "skipped": 0, "errors": 0}
