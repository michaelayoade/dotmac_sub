"""Asynchronous Huawei ONT firmware apply and post-reboot verification."""

from __future__ import annotations

import logging
from typing import Any

from app.celery_app import celery_app
from app.models.network_operation import NetworkOperationStatus
from app.services.db_session_adapter import db_session_adapter
from app.services.network.ont_firmware import (
    normalized_version,
    operation_is_active,
    validate_image_compatibility,
)
from app.services.network_operation_dispatch import managed_network_operation_dispatch
from app.services.network_operations import network_operations
from app.services.queue_adapter import enqueue_task

logger = logging.getLogger(__name__)

VERIFY_TASK_NAME = "app.tasks.ont_firmware.verify_huawei_ont_firmware"
VERIFY_DELAY_SECONDS = 120
MAX_VERIFY_ATTEMPTS = 15


def _fail_operation(db, operation_id: str, message: str) -> dict[str, Any]:
    operation = network_operations.get(db, operation_id)
    if operation_is_active(operation):
        network_operations.mark_failed(
            db,
            operation_id,
            message,
            output_payload={"verified": False, "message": message},
        )
        db.commit()
    return {"success": False, "operation_id": operation_id, "message": message}


def _queue_verification(operation, ont_id: str, firmware_image_id: str, attempt: int):
    return enqueue_task(
        VERIFY_TASK_NAME,
        args=(ont_id, firmware_image_id, str(operation.id), attempt),
        countdown=VERIFY_DELAY_SECONDS,
        correlation_id=operation.correlation_key,
        source="ont_firmware_verify",
    )


@celery_app.task(
    name="app.tasks.ont_firmware.apply_huawei_ont_firmware",
    soft_time_limit=540,
    time_limit=600,
)
@managed_network_operation_dispatch("app.tasks.ont_firmware.apply_huawei_ont_firmware")
def apply_huawei_ont_firmware(
    ont_id: str,
    firmware_image_id: str,
    operation_id: str,
    *,
    _network_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Deliver the image, then hand ownership to the readback task."""
    from app.models.network import OntFirmwareImage, OntUnit
    from app.services.network.ont_action_common import get_ont_client_or_error

    with db_session_adapter.session() as db:
        operation = network_operations.get(db, operation_id)
        if not operation_is_active(operation):
            return {
                "success": operation.status == NetworkOperationStatus.succeeded,
                "operation_id": operation_id,
                "status": operation.status.value,
            }
        prior_phase = str((operation.output_payload or {}).get("phase") or "")
        if prior_phase in {"delivery_started", "delivery_accepted"}:
            if operation.status != NetworkOperationStatus.waiting:
                network_operations.mark_waiting(
                    db,
                    operation_id,
                    "Recovering firmware readback after an interrupted worker run.",
                )
                db.commit()
            dispatch = _queue_verification(
                operation,
                ont_id,
                firmware_image_id,
                int(operation.retry_count or 0),
            )
            if not dispatch.queued:
                return _fail_operation(
                    db,
                    operation_id,
                    dispatch.error or "Unable to queue firmware recovery readback.",
                )
            return {
                "success": True,
                "waiting": True,
                "operation_id": operation_id,
                "recovered": True,
            }
        ont = db.get(OntUnit, ont_id)
        image = db.get(OntFirmwareImage, firmware_image_id)
        if ont is None:
            return _fail_operation(db, operation_id, "ONT not found.")
        if image is None or not image.is_active:
            return _fail_operation(
                db, operation_id, "Firmware image is missing or inactive."
            )
        compatibility_error = validate_image_compatibility(ont, image)
        if compatibility_error:
            return _fail_operation(db, operation_id, compatibility_error)

        resolved, error = get_ont_client_or_error(db, ont_id)
        if error or resolved is None:
            return _fail_operation(
                db,
                operation_id,
                error.message if error else "ONT ACS resolution failed.",
            )
        _, client, device_id = resolved
        operation = network_operations.mark_running(db, operation_id)
        operation.output_payload = {
            "phase": "delivery_started",
            "delivery_accepted": False,
            "verified": False,
            "target_version": image.version,
        }
        db.commit()

        try:
            task_result = client.download_and_wait(
                device_id,
                file_type="1 Firmware Upgrade Image",
                file_url=image.file_url,
                filename=image.filename,
                timeout_sec=480,
            )
        except Exception as exc:  # noqa: BLE001 - device/ACS errors become ledger state
            logger.exception("Huawei ONT firmware delivery failed for %s", ont_id)
            return _fail_operation(db, operation_id, f"Firmware delivery failed: {exc}")

        operation = network_operations.mark_waiting(
            db,
            operation_id,
            "Firmware image accepted; waiting for reboot and ACS version readback.",
        )
        operation.output_payload = {
            "phase": "delivery_accepted",
            "delivery_accepted": True,
            "verified": False,
            "target_version": image.version,
            "acs_task": task_result,
        }
        db.commit()

        dispatch = _queue_verification(operation, ont_id, firmware_image_id, 0)
        if not dispatch.queued:
            return _fail_operation(
                db,
                operation_id,
                dispatch.error or "Unable to queue firmware verification.",
            )
        return {
            "success": True,
            "waiting": True,
            "operation_id": operation_id,
            "target_version": image.version,
        }


@celery_app.task(
    name=VERIFY_TASK_NAME,
    soft_time_limit=120,
    time_limit=150,
)
def verify_huawei_ont_firmware(
    ont_id: str,
    firmware_image_id: str,
    operation_id: str,
    attempt: int = 0,
) -> dict[str, Any]:
    """Retry ACS readback until the post-reboot version matches or times out."""
    from app.models.network import OntFirmwareImage, OntUnit
    from app.services.network.ont_action_common import get_ont_client_or_error
    from app.services.network.reconcile.adapters import desired_from_ont_unit
    from app.services.network.reconcile.readers import read_acs_state

    with db_session_adapter.session() as db:
        operation = network_operations.get(db, operation_id)
        if not operation_is_active(operation):
            return {
                "success": operation.status == NetworkOperationStatus.succeeded,
                "operation_id": operation_id,
                "status": operation.status.value,
            }
        ont = db.get(OntUnit, ont_id)
        image = db.get(OntFirmwareImage, firmware_image_id)
        if ont is None or image is None:
            return _fail_operation(
                db, operation_id, "ONT or firmware image no longer exists."
            )

        observed_version: str | None = None
        read_error: str | None = None
        resolved, error = get_ont_client_or_error(db, ont_id)
        if resolved is not None:
            _, client, _device_id = resolved
            read_result = read_acs_state(client, desired_from_ont_unit(db, ont))
            if read_result.observed is not None:
                observed_version = read_result.observed.acs_observed_software_version
            read_error = read_result.error
        elif error is not None:
            read_error = error.message

        if observed_version and normalized_version(
            observed_version
        ) == normalized_version(image.version):
            ont.software_version = observed_version
            ont.firmware_version = observed_version
            network_operations.mark_succeeded(
                db,
                operation_id,
                output_payload={
                    "delivery_accepted": True,
                    "verified": True,
                    "target_version": image.version,
                    "observed_version": observed_version,
                    "verification_attempts": attempt + 1,
                },
            )
            db.commit()
            return {
                "success": True,
                "verified": True,
                "operation_id": operation_id,
                "observed_version": observed_version,
            }

        next_attempt = attempt + 1
        operation.retry_count = next_attempt
        reason = (
            f"Waiting for firmware v{image.version}; ACS reports "
            f"{observed_version or 'no version yet'}."
        )
        if read_error:
            reason = f"Waiting for ACS readback: {read_error}"
        if next_attempt >= MAX_VERIFY_ATTEMPTS:
            return _fail_operation(
                db,
                operation_id,
                f"Firmware verification timed out. {reason}",
            )

        network_operations.mark_waiting(db, operation_id, reason)
        db.commit()
        dispatch = _queue_verification(
            operation, ont_id, firmware_image_id, next_attempt
        )
        if not dispatch.queued:
            return _fail_operation(
                db,
                operation_id,
                dispatch.error or "Unable to queue the next firmware verification.",
            )
        return {
            "success": True,
            "waiting": True,
            "operation_id": operation_id,
            "attempt": next_attempt,
            "observed_version": observed_version,
        }
