"""Celery tasks for ONT provisioning."""

import logging
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_provisioning.authorize_ont")
def authorize_ont(
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    force_reauthorize: bool = False,
    preset_id: str | None = None,
    scoped_ont_id: str | None = None,
    initiated_by: str | None = None,
) -> dict[str, Any]:
    """Authorize an ONT outside the web request timeout path."""
    operation_id: str | None = None
    target_id = str(scoped_ont_id or olt_id)

    with db_session_adapter.session() as db:
        try:
            from app.models.network_operation import (
                NetworkOperationTargetType,
                NetworkOperationType,
            )
            from app.services.network.ont_authorization import (
                authorize_ont as run_authorization,
            )
            from app.services.network_operations import network_operations

            target_type = (
                NetworkOperationTargetType.ont
                if scoped_ont_id
                else NetworkOperationTargetType.olt
            )
            op = network_operations.start(
                db,
                NetworkOperationType.ont_authorize,
                target_type,
                target_id,
                correlation_key=f"ont_authorize:{olt_id}:{fsp}:{serial_number}",
                input_payload={
                    "olt_id": olt_id,
                    "fsp": fsp,
                    "serial_number": serial_number,
                    "force_reauthorize": force_reauthorize,
                    "preset_id": preset_id,
                    "scoped_ont_id": scoped_ont_id,
                },
                initiated_by=initiated_by or "system",
            )
            operation_id = str(op.id)
            network_operations.mark_running(db, operation_id)
            db.commit()

            result = run_authorization(
                db,
                olt_id,
                fsp,
                serial_number,
                force_reauthorize=force_reauthorize,
                preset_id=preset_id,
                request=None,
            )
            payload = result.to_dict()
            payload["operation_id"] = operation_id

            if result.success:
                network_operations.mark_succeeded(
                    db, operation_id, output_payload=payload
                )
            elif result.partial_success:
                network_operations.mark_warning(
                    db,
                    operation_id,
                    result.message,
                    output_payload=payload,
                )
            else:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    result.message,
                    output_payload=payload,
                )
            db.commit()
            return payload
        except Exception as exc:
            logger.exception(
                "Background ONT authorization failed olt_id=%s fsp=%s serial=%s",
                olt_id,
                fsp,
                serial_number,
            )
            if operation_id:
                try:
                    from app.services.network_operations import network_operations

                    network_operations.mark_failed(db, operation_id, str(exc))
                    db.commit()
                except Exception:
                    logger.exception(
                        "Failed to mark ONT authorization operation failed"
                    )
            return {
                "success": False,
                "message": f"Authorization task error: {exc}",
                "operation_id": operation_id,
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
            }


@celery_app.task(name="app.tasks.ont_provisioning.provision_ont")
def provision_ont(
    ont_id: str,
    *,
    dry_run: bool = False,
    initiated_by: str | None = None,
    correlation_key: str | None = None,
    bulk_run_id: str | None = None,
    bulk_item_id: str | None = None,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
) -> dict[str, Any]:
    """Repair/re-apply OLT authorization baseline for one ONT.

    Normal authorization applies this baseline automatically. The ACS flags are
    retained for backward-compatible task payloads and intentionally ignored.
    """
    del wait_for_acs, apply_acs_config
    del initiated_by  # Reserved for audit propagation when needed.
    with db_session_adapter.session() as db:
        if bulk_item_id:
            from app.services.network.bulk_provisioning import mark_bulk_item_running

            mark_bulk_item_running(db, bulk_item_id)
            db.commit()

        try:
            from app.services.network.ont_provision_steps import (
                apply_authorization_baseline,
            )
            from app.services.network.provisioning_events import (
                provisioning_correlation,
            )

            effective_correlation = correlation_key or f"provision:{ont_id}"
            with provisioning_correlation(effective_correlation):
                result = apply_authorization_baseline(
                    db,
                    ont_id,
                    dry_run=dry_run,
                    allow_low_optical_margin=allow_low_optical_margin,
                )
            payload = {
                "success": result.success,
                "message": result.message,
                "ont_id": ont_id,
                "duration_ms": result.duration_ms,
                "step_name": result.step_name,
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": bulk_item_id,
                "correlation_key": effective_correlation,
            }
            if bulk_item_id:
                from app.services.network.bulk_provisioning import (
                    mark_bulk_item_completed,
                )

                mark_bulk_item_completed(db, bulk_item_id, payload)
                db.commit()
            return payload
        except Exception as exc:
            logger.exception("ONT provisioning task failed for %s", ont_id)
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
    dry_run: bool = False,
    initiated_by: str | None = None,
    max_parallel: int = 10,
    chunk_delay_seconds: int = 15,
    bulk_run_id: str | None = None,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
) -> dict[str, Any]:
    """Repair/re-apply OLT authorization baseline for many ONTs synchronously.

    Normal authorization applies this baseline automatically. The ACS flags are
    retained for backward-compatible task payloads and intentionally ignored.
    """
    del wait_for_acs, apply_acs_config
    bulk_items_by_ont_id: dict[str, Any] = {}
    if bulk_run_id:
        with db_session_adapter.read_session() as session:
            from app.services.network.bulk_provisioning import list_pending_bulk_items

            pending_items = list_pending_bulk_items(session, bulk_run_id)
            bulk_items_by_ont_id = {
                str(item.ont_unit_id): item
                for item in pending_items
                if item.ont_unit_id
            }

    unique_ont_ids = list(dict.fromkeys(str(ont_id) for ont_id in ont_ids if ont_id))
    if bulk_items_by_ont_id:
        unique_ont_ids = [
            ont_id for ont_id in unique_ont_ids if ont_id in bulk_items_by_ont_id
        ]
    if not unique_ont_ids:
        return {
            "processed": 0,
            "errors": 0,
            "skipped": 0,
            "message": "No ONTs supplied.",
            "tasks": [],
        }

    del max_parallel, chunk_delay_seconds  # No longer used
    tasks: list[dict[str, Any]] = []
    errors = 0
    failed_results = 0

    from app.services.network.ont_provision_steps import apply_authorization_baseline
    from app.services.network.provisioning_events import provisioning_correlation

    for ont_id in unique_ont_ids:
        bulk_item = bulk_items_by_ont_id.get(ont_id)
        item_correlation_key = (
            getattr(bulk_item, "correlation_key", None)
            if bulk_item is not None
            else f"bulk_provision:{ont_id}"
        )
        try:
            with db_session_adapter.session() as db:
                if bulk_item is not None:
                    from app.services.network.bulk_provisioning import (
                        mark_bulk_item_completed,
                        mark_bulk_item_running,
                    )

                    mark_bulk_item_running(db, bulk_item.id)
                    db.flush()

                with provisioning_correlation(item_correlation_key):
                    result = apply_authorization_baseline(
                        db,
                        ont_id,
                        dry_run=dry_run,
                        allow_low_optical_margin=allow_low_optical_margin,
                    )
                payload = {
                    "success": result.success,
                    "message": result.message,
                    "ont_id": ont_id,
                    "duration_ms": result.duration_ms,
                    "bulk_run_id": bulk_run_id,
                    "bulk_item_id": str(bulk_item.id)
                    if bulk_item is not None
                    else None,
                    "correlation_key": item_correlation_key,
                }
                if bulk_item is not None:
                    mark_bulk_item_completed(db, bulk_item.id, payload)
                db.commit()
            if not result.success:
                failed_results += 1
            tasks.append(
                {
                    "ont_id": ont_id,
                    "bulk_item_id": str(bulk_item.id) if bulk_item is not None else "",
                    "correlation_key": item_correlation_key,
                    "success": result.success,
                    "message": result.message,
                }
            )
        except Exception as exc:
            errors += 1
            if bulk_item is not None:
                with db_session_adapter.session() as db:
                    from app.services.network.bulk_provisioning import (
                        mark_bulk_item_failed,
                    )

                    mark_bulk_item_failed(db, bulk_item.id, str(exc))
                    db.commit()
            tasks.append(
                {
                    "ont_id": ont_id,
                    "bulk_item_id": str(bulk_item.id) if bulk_item is not None else "",
                    "correlation_key": item_correlation_key,
                    "success": False,
                    "error": str(exc),
                }
            )

    stats = {
        "processed": len(tasks) - errors,
        "errors": errors + failed_results,
        "exceptions": errors,
        "failed": failed_results,
        "skipped": len(ont_ids) - len(unique_ont_ids),
        "bulk_run_id": bulk_run_id,
        "tasks": tasks,
    }
    logger.info("Bulk provisioning executed: %s", stats)
    return stats
