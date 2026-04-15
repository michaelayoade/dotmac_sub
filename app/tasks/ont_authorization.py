"""Background ONT authorization follow-up tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ont_authorization.run_post_authorization_follow_up")
def run_post_authorization_follow_up_task(
    operation_id: str,
    ont_unit_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    ont_id_on_olt: int,
) -> dict[str, object]:
    """Run non-critical reconciliation after foreground OLT authorization succeeds."""
    from app.services.network.olt_authorization_workflow import (
        run_post_authorization_follow_up,
    )
    from app.services.network_operations import network_operations

    db = SessionLocal()
    try:
        network_operations.mark_running(db, operation_id)
        db.commit()

        try:
            success, message, steps = run_post_authorization_follow_up(
                db,
                ont_unit_id=ont_unit_id,
                olt_id=olt_id,
                fsp=fsp,
                serial_number=serial_number,
                ont_id_on_olt=ont_id_on_olt,
            )
            payload = {"message": message, "steps": steps}
            if success:
                network_operations.mark_succeeded(
                    db,
                    operation_id,
                    output_payload=payload,
                )
            else:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    message,
                    output_payload=payload,
                )
            return {"success": success, "message": message, "steps": steps}
        except Exception as exc:
            logger.error(
                "Post-authorization follow-up failed for ONT %s: %s",
                ont_unit_id,
                exc,
                exc_info=True,
            )
            network_operations.mark_failed(db, operation_id, str(exc))
            raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.ont_authorization.run_authorize_autofind_ont")
def run_authorize_autofind_ont_task(
    operation_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
) -> dict[str, object]:
    """Run the full OLT autofind authorization workflow in the background."""
    from app.models.network import OLTDevice
    from app.services.network.olt_authorization_workflow import (
        authorize_autofind_ont_and_provision_network_audited,
    )
    from app.services.network_operations import network_operations
    from app.services.operation_notifications import publish_operation_status

    db = SessionLocal()
    olt_name = None
    start_time = None
    try:
        import time
        start_time = time.time()

        # Get OLT name for notifications
        olt = db.get(OLTDevice, olt_id)
        olt_name = olt.name if olt else None

        network_operations.mark_running(db, operation_id)
        db.commit()

        # Notify UI that authorization is running
        publish_operation_status(
            operation_id,
            "running",
            f"Authorizing ONT {serial_number} on {olt_name or 'OLT'}...",
            target_id=olt_id,
            target_name=olt_name,
            extra={"fsp": fsp, "serial_number": serial_number},
        )

        result = authorize_autofind_ont_and_provision_network_audited(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            request=None,
        )
        payload = result.to_dict()
        duration_ms = int((time.time() - start_time) * 1000) if start_time else None

        if result.success:
            network_operations.mark_succeeded(
                db,
                operation_id,
                output_payload=payload,
            )
            # Notify UI of success with ONT link info
            publish_operation_status(
                operation_id,
                "succeeded",
                result.message,
                target_id=olt_id,
                target_name=olt_name,
                duration_ms=duration_ms,
                extra={
                    "fsp": fsp,
                    "serial_number": serial_number,
                    "ont_id_on_olt": payload.get("ont_id_on_olt"),
                    "ont_unit_id": payload.get("ont_unit_id"),
                    "view_url": f"/admin/network/onts/{payload.get('ont_unit_id')}"
                    if payload.get("ont_unit_id")
                    else None,
                },
            )
        else:
            network_operations.mark_failed(
                db,
                operation_id,
                result.message,
                output_payload=payload,
            )
            # Notify UI of failure
            publish_operation_status(
                operation_id,
                "failed",
                result.message,
                target_id=olt_id,
                target_name=olt_name,
                duration_ms=duration_ms,
                extra={"fsp": fsp, "serial_number": serial_number},
            )
        db.commit()
        return payload
    except Exception as exc:
        db.rollback()
        logger.error(
            "ONT authorization task failed for serial %s on OLT %s: %s",
            serial_number,
            olt_id,
            exc,
            exc_info=True,
        )
        # Notify UI of error
        publish_operation_status(
            operation_id,
            "failed",
            f"Authorization failed: {exc}",
            target_id=olt_id,
            target_name=olt_name,
            extra={"fsp": fsp, "serial_number": serial_number, "error": str(exc)},
        )
        try:
            network_operations.mark_failed(db, operation_id, str(exc))
            db.commit()
        except Exception:
            db.rollback()
        raise
    finally:
        db.close()
