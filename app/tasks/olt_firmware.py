"""Background OLT firmware upgrade tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_firmware.upgrade_with_verification")
@idempotent_task(
    key_func=lambda olt_id, image_id, **kw: f"firmware_upgrade:{olt_id}:{image_id}"
)
def upgrade_firmware_task(
    olt_id: str,
    image_id: str,
    *,
    verify_after: bool = True,
    timeout_sec: int = 300,
    poll_interval_sec: int = 15,
    initial_wait_sec: int = 60,
    operation_id: str | None = None,
) -> dict[str, object]:
    """Run firmware upgrade with verification in background.

    Args:
        olt_id: UUID of the OLT.
        image_id: UUID of the firmware image.
        verify_after: Whether to verify OLT is reachable after upgrade.
        timeout_sec: Total timeout for reachability polling.
        poll_interval_sec: Interval between reachability checks.
        initial_wait_sec: Initial wait before first check (for reboot).
        operation_id: Optional network operation ID for tracking.

    Returns:
        Dict with upgrade result details.
    """
    from app.services.network.olt_firmware import upgrade_with_verification_audited

    db = SessionLocal()
    try:
        # Mark operation as running if tracked
        if operation_id:
            from app.services.network_operations import network_operations

            try:
                network_operations.mark_running(db, operation_id)
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Could not mark operation %s as running: %s",
                    operation_id,
                    exc,
                )
                db.rollback()

        # Run the upgrade
        result = upgrade_with_verification_audited(
            db,
            olt_id,
            image_id,
            dry_run=False,
            verify_after=verify_after,
            timeout_sec=timeout_sec,
        )

        # Update operation status
        if operation_id:
            from app.services.network_operations import network_operations

            try:
                if result.success:
                    network_operations.mark_succeeded(
                        db,
                        operation_id,
                        output_payload=result.to_dict(),
                    )
                else:
                    network_operations.mark_failed(
                        db,
                        operation_id,
                        result.message,
                        output_payload=result.to_dict(),
                    )
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Could not update operation %s status: %s",
                    operation_id,
                    exc,
                )
                db.rollback()

        logger.info(
            "Firmware upgrade task completed for OLT %s: success=%s message=%s",
            olt_id,
            result.success,
            result.message,
        )

        return result.to_dict()
    except Exception as exc:
        logger.error(
            "Firmware upgrade task failed for OLT %s: %s",
            olt_id,
            exc,
            exc_info=True,
        )

        # Mark operation as failed
        if operation_id:
            from app.services.network_operations import network_operations

            try:
                network_operations.mark_failed(
                    db,
                    operation_id,
                    str(exc),
                )
                db.commit()
            except Exception:
                db.rollback()

        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.olt_firmware.rollback")
def rollback_firmware_task(
    olt_id: str,
    *,
    operation_id: str | None = None,
) -> dict[str, object]:
    """Rollback firmware to standby image.

    Args:
        olt_id: UUID of the OLT.
        operation_id: Optional network operation ID for tracking.

    Returns:
        Dict with rollback result details.
    """
    from app.services.network import olt_ssh as olt_ssh_service
    from app.services.network.olt_inventory import get_olt_or_none
    from app.services.network.olt_web_audit import log_olt_audit_event

    db = SessionLocal()
    try:
        olt = get_olt_or_none(db, olt_id)
        if not olt:
            result = {"success": False, "message": "OLT not found"}
            return result

        success, message = olt_ssh_service.rollback_firmware(olt)

        log_olt_audit_event(
            db,
            request=None,
            action="firmware_rollback",
            entity_id=olt_id,
            metadata={
                "result": "success" if success else "error",
                "message": message,
            },
            status_code=200 if success else 500,
            is_success=success,
        )
        db.commit()

        logger.info(
            "Firmware rollback task completed for OLT %s: success=%s message=%s",
            olt_id,
            success,
            message,
        )

        return {"success": success, "message": message}
    except Exception as exc:
        db.rollback()
        logger.error(
            "Firmware rollback task failed for OLT %s: %s",
            olt_id,
            exc,
            exc_info=True,
        )
        raise
    finally:
        db.close()
