"""Background OLT autofind scan tasks with per-OLT progress tracking."""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.network import OLTDevice
from app.services.db_session_adapter import db_session_adapter
from app.services.operation_notifications import publish_operation_status

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_autofind.scan_olts_autofind")
def scan_olts_autofind(
    operation_id: str,
    olt_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Scan OLTs for unconfigured ONTs with per-OLT progress updates.

    This task scans each OLT sequentially and sends WebSocket progress updates
    so the UI can show real-time per-OLT status.

    Args:
        operation_id: The NetworkOperation ID for tracking progress.
        olt_ids: Optional list of specific OLT IDs to scan. If None, scans all active OLTs.

    Returns:
        Dict with scan results: totals, per-OLT results, failures.
    """
    from app.services import web_network_ont_autofind as autofind_service
    from app.services.network_operations import network_operations

    db = db_session_adapter.create_session()

    try:
        # Mark operation as running
        network_operations.mark_running(db, operation_id)
        db.commit()

        # Get OLTs to scan
        query = select(OLTDevice).where(OLTDevice.is_active.is_(True))
        if olt_ids:
            query = query.where(OLTDevice.id.in_(olt_ids))
        query = query.order_by(OLTDevice.name.asc())
        olts = list(db.scalars(query).all())

        if not olts:
            network_operations.mark_succeeded(
                db,
                operation_id,
                output_payload={
                    "message": "No active OLTs found to scan",
                    "scanned": 0,
                    "olt_results": [],
                },
            )
            db.commit()
            publish_operation_status(
                operation_id,
                "warning",
                "No active OLTs found to scan",
                operation_type="autofind_scan",
            )
            return {"success": True, "message": "No active OLTs found", "scanned": 0}

        # Publish initial progress
        publish_operation_status(
            operation_id,
            "running",
            f"Starting scan of {len(olts)} OLT(s)...",
            operation_type="autofind_scan",
            extra={
                "total_olts": len(olts),
                "completed_olts": 0,
                "olt_results": [],
            },
        )

        # Track results
        olt_results: list[dict[str, Any]] = []
        total_created = 0
        total_updated = 0
        total_resolved = 0
        failed_olts: list[str] = []

        for idx, olt in enumerate(olts):
            olt_name = olt.name or str(olt.id)
            start_time = time.time()

            # Publish "scanning" status for this OLT
            publish_operation_status(
                operation_id,
                "running",
                f"Scanning {olt_name}...",
                operation_type="autofind_scan",
                extra={
                    "total_olts": len(olts),
                    "completed_olts": idx,
                    "current_olt": {
                        "id": str(olt.id),
                        "name": olt_name,
                        "status": "scanning",
                    },
                    "olt_results": olt_results,
                },
            )

            try:
                ok, message, stats = autofind_service.sync_olt_autofind_candidates(
                    db,
                    str(olt.id),
                )
                duration_ms = int((time.time() - start_time) * 1000)

                if ok:
                    db.commit()
                    created = int(stats.get("created", 0))
                    updated = int(stats.get("updated", 0))
                    resolved = int(stats.get("resolved", 0))
                    found_count = created + updated

                    total_created += created
                    total_updated += updated
                    total_resolved += resolved

                    olt_results.append({
                        "id": str(olt.id),
                        "name": olt_name,
                        "status": "success",
                        "found": found_count,
                        "created": created,
                        "updated": updated,
                        "resolved": resolved,
                        "duration_ms": duration_ms,
                        "message": f"{found_count} found",
                    })
                else:
                    db.rollback()
                    failed_olts.append(f"{olt_name}: {message}")
                    olt_results.append({
                        "id": str(olt.id),
                        "name": olt_name,
                        "status": "failed",
                        "found": 0,
                        "duration_ms": duration_ms,
                        "message": message,
                    })

            except Exception as exc:
                db.rollback()
                duration_ms = int((time.time() - start_time) * 1000)
                error_msg = str(exc)
                logger.warning(
                    "Autofind scan failed for OLT %s: %s",
                    olt_name,
                    exc,
                    exc_info=True,
                )
                failed_olts.append(f"{olt_name}: {error_msg}")
                olt_results.append({
                    "id": str(olt.id),
                    "name": olt_name,
                    "status": "failed",
                    "found": 0,
                    "duration_ms": duration_ms,
                    "message": error_msg[:100],
                })

            # Publish progress after each OLT
            publish_operation_status(
                operation_id,
                "running",
                f"Scanned {idx + 1}/{len(olts)} OLTs",
                operation_type="autofind_scan",
                extra={
                    "total_olts": len(olts),
                    "completed_olts": idx + 1,
                    "olt_results": olt_results,
                },
            )

        # Build final summary
        scanned_count = len(olts) - len(failed_olts)
        if failed_olts and scanned_count == 0:
            final_status = "failed"
            final_message = f"Autofind scan failed: {'; '.join(failed_olts[:3])}"
        elif failed_olts:
            final_status = "warning"
            final_message = (
                f"Scanned {scanned_count} OLT(s): {total_created} new, "
                f"{total_updated} refreshed, {total_resolved} resolved. "
                f"Failed: {'; '.join(failed_olts[:3])}"
            )
        else:
            final_status = "succeeded"
            final_message = (
                f"Scanned {scanned_count} OLT(s): {total_created} new, "
                f"{total_updated} refreshed, {total_resolved} resolved"
            )

        output_payload = {
            "message": final_message,
            "scanned": scanned_count,
            "created": total_created,
            "updated": total_updated,
            "resolved": total_resolved,
            "failed": failed_olts,
            "olt_results": olt_results,
        }

        if final_status == "failed":
            network_operations.mark_failed(
                db, operation_id, final_message, output_payload=output_payload
            )
        else:
            network_operations.mark_succeeded(
                db, operation_id, output_payload=output_payload
            )
        db.commit()

        # Publish final status
        publish_operation_status(
            operation_id,
            final_status,  # type: ignore[arg-type]
            final_message,
            operation_type="autofind_scan",
            extra={
                "total_olts": len(olts),
                "completed_olts": len(olts),
                "olt_results": olt_results,
                "summary": {
                    "scanned": scanned_count,
                    "created": total_created,
                    "updated": total_updated,
                    "resolved": total_resolved,
                    "failed_count": len(failed_olts),
                },
            },
        )

        return {
            "success": final_status != "failed",
            "message": final_message,
            "scanned": scanned_count,
            "created": total_created,
            "updated": total_updated,
            "resolved": total_resolved,
            "failed": failed_olts,
            "olt_results": olt_results,
        }

    except Exception as exc:
        db.rollback()
        logger.exception("Autofind scan task failed: %s", exc)
        try:
            network_operations.mark_failed(db, operation_id, str(exc))
            db.commit()
        except Exception:
            pass
        publish_operation_status(
            operation_id,
            "failed",
            f"Scan failed: {exc}",
            operation_type="autofind_scan",
        )
        raise
    finally:
        db.close()
