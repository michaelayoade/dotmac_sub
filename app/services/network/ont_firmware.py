"""Tracked Huawei ONT firmware upgrade intent and dispatch."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.network import OntFirmwareImage
from app.models.network_operation import (
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network.ont_action_common import ActionResult, get_ont_strict_or_error
from app.services.network_operations import network_operations
from app.services.queue_adapter import enqueue_task

FIRMWARE_TASK_NAME = "app.tasks.ont_firmware.apply_huawei_ont_firmware"


def normalized_version(value: object) -> str:
    return str(value or "").strip().lower().removeprefix("v")


def validate_image_compatibility(ont, image: OntFirmwareImage) -> str | None:
    ont_vendor = str(getattr(ont, "vendor", "") or "").strip().lower()
    image_vendor = str(image.vendor or "").strip().lower()
    if ont_vendor and image_vendor != ont_vendor:
        return f"Firmware vendor {image.vendor} does not match ONT vendor {ont.vendor}."

    ont_model = str(getattr(ont, "model", "") or "").strip().lower()
    image_model = str(image.model or "").strip().lower()
    if image_model and (not ont_model or image_model != ont_model):
        return f"Firmware model {image.model} does not match ONT model {ont.model}."
    return None


def request_firmware_upgrade(
    db: Session,
    ont_id: str,
    firmware_image_id: str,
    *,
    initiated_by: str | None = None,
) -> ActionResult:
    """Persist one deduplicated upgrade intent and enqueue device work."""
    ont, error = get_ont_strict_or_error(db, ont_id)
    if error or ont is None:
        return error or ActionResult(success=False, message="ONT not found.")

    image = db.get(OntFirmwareImage, firmware_image_id)
    if image is None:
        return ActionResult(success=False, message="Firmware image not found.")
    if not image.is_active:
        return ActionResult(success=False, message="Firmware image is not active.")
    compatibility_error = validate_image_compatibility(ont, image)
    if compatibility_error:
        return ActionResult(success=False, message=compatibility_error)

    current_version = str(
        getattr(ont, "software_version", None)
        or getattr(ont, "firmware_version", None)
        or ""
    ).strip()
    if normalized_version(current_version) == normalized_version(image.version):
        return ActionResult(
            success=True,
            message=f"ONT already reports firmware v{image.version}.",
            data={"verified": True, "firmware_version": image.version},
        )

    try:
        operation = network_operations.start(
            db,
            NetworkOperationType.ont_firmware_upgrade,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=f"ont_firmware_upgrade:{ont_id}",
            input_payload={
                "firmware_image_id": str(image.id),
                "target_version": image.version,
                "previous_version": current_version or None,
                "vendor": image.vendor,
                "model": image.model,
                "checksum": image.checksum,
            },
            initiated_by=initiated_by,
        )
        db.commit()
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return ActionResult(
            success=True,
            waiting=True,
            message="A firmware upgrade is already in progress for this ONT.",
        )

    dispatch = enqueue_task(
        FIRMWARE_TASK_NAME,
        args=(ont_id, str(image.id), str(operation.id)),
        correlation_id=operation.correlation_key,
        source="ont_firmware_upgrade",
        actor_id=initiated_by,
    )
    if not dispatch.queued:
        network_operations.mark_failed(
            db,
            str(operation.id),
            dispatch.error or "Unable to queue firmware upgrade.",
        )
        db.commit()
        return ActionResult(
            success=False,
            message="Unable to queue firmware upgrade.",
            data={"operation_id": str(operation.id)},
        )

    return ActionResult(
        success=True,
        waiting=True,
        message=(
            f"Firmware v{image.version} is queued. Completion will be recorded "
            "only after post-reboot ACS readback."
        ),
        data={
            "operation_id": str(operation.id),
            "task_id": dispatch.task_id,
            "target_version": image.version,
            "verified": False,
        },
    )


def operation_is_active(operation) -> bool:
    return operation.status in {
        NetworkOperationStatus.pending,
        NetworkOperationStatus.running,
        NetworkOperationStatus.waiting,
    }
