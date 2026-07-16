"""Background OLT firmware upgrade tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operation_dispatch import managed_network_operation_dispatch
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_firmware.upgrade_with_verification")
@managed_network_operation_dispatch("app.tasks.olt_firmware.upgrade_with_verification")
@idempotent_task(
    key_func=lambda olt_id, image_id, **kw: (
        f"firmware_upgrade:{olt_id}:{image_id}:{kw.get('operation_id') or 'untracked'}"
    )
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
    _network_dispatch_id: str | None = None,
) -> dict[str, object]:
    """Run firmware upgrade with verification in background.

    Args:
        olt_id: UUID of the OLT.
        image_id: UUID of the firmware image.
        verify_after: Whether to verify OLT is reachable after upgrade.
        timeout_sec: Total timeout for reachability polling.
        poll_interval_sec: Interval between reachability checks.
        initial_wait_sec: Initial wait before first check (for reboot).
        operation_id: Required network operation ID that owns this write.

    Returns:
        Dict with upgrade result details.
    """
    from app.services.network.olt_firmware import upgrade_with_verification_audited

    db = db_session_adapter.create_session()
    try:
        if not operation_id:
            return {
                "success": False,
                "message": "Tracked OLT firmware operation is required.",
            }

        from app.models.network_operation import (
            NetworkOperationTargetType,
            NetworkOperationType,
        )
        from app.services.network.olt_firmware import operation_is_active
        from app.services.network_operations import network_operations

        operation = network_operations.get(db, operation_id)
        if not operation_is_active(operation):
            return {
                "success": operation.status.value == "succeeded",
                "operation_id": operation_id,
                "status": operation.status.value,
            }
        input_payload = operation.input_payload or {}
        operation_matches = (
            operation.operation_type == NetworkOperationType.olt_firmware_upgrade
            and operation.target_type == NetworkOperationTargetType.olt
            and str(operation.target_id) == str(olt_id)
            and str(input_payload.get("firmware_image_id")) == str(image_id)
        )
        if not operation_matches:
            message = "Firmware task arguments do not match the tracked operation."
            network_operations.mark_failed(db, operation_id, message)
            db.commit()
            return {
                "success": False,
                "operation_id": operation_id,
                "message": message,
            }
        network_operations.mark_running(db, operation_id)
        db.commit()

        def _mark_waiting(reason: str) -> None:
            from app.services.network_operations import network_operations

            waiting_operation = network_operations.mark_waiting(
                db, operation_id, reason
            )
            waiting_operation.output_payload = {
                "phase": "reboot_wait",
                "verified": False,
                "message": reason,
            }
            db.commit()

        result = upgrade_with_verification_audited(
            db,
            olt_id,
            image_id,
            dry_run=False,
            verify_after=verify_after,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            initial_wait_sec=initial_wait_sec,
            on_waiting=_mark_waiting,
        )

        # Update operation status
        if operation_id:
            from app.services.network_operations import network_operations

            try:
                if result.success:
                    if result.verified_version:
                        from app.models.network import OLTDevice

                        olt = db.get(OLTDevice, olt_id)
                        if olt is None:
                            network_operations.mark_failed(
                                db,
                                operation_id,
                                "OLT disappeared before firmware SOT commit.",
                                output_payload=result.to_dict(),
                            )
                        else:
                            olt.firmware_version = result.verified_version
                            network_operations.mark_succeeded(
                                db,
                                operation_id,
                                output_payload={**result.to_dict(), "verified": True},
                            )
                    else:
                        network_operations.mark_warning(
                            db,
                            operation_id,
                            "Firmware command completed without version readback.",
                            output_payload={**result.to_dict(), "verified": False},
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
                logger.exception(
                    "Could not atomically commit firmware readback for operation %s: %s",
                    operation_id,
                    exc,
                )
                db.rollback()
                raise

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

    db = db_session_adapter.create_session()
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
