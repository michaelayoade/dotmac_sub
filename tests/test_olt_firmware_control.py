"""Tracked Huawei OLT firmware control-plane tests."""

from __future__ import annotations

import inspect

from app.models.network import OLTDevice, OltFirmwareImage
from app.models.network_operation import (
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network import olt_firmware
from app.services.network.olt_firmware import (
    FirmwareUpgradeResult,
    request_firmware_upgrade,
    upgrade_with_verification,
)
from app.services.network.parsers.firmware import FirmwareInfo
from app.services.network_operations import network_operations
from app.services.queue_adapter import QueueDispatchResult


def _inventory(db_session):
    olt = OLTDevice(
        name="Huawei Core OLT",
        vendor="Huawei",
        model="MA5800-X7",
        firmware_version="V1R1",
        mgmt_ip="192.0.2.20",
    )
    image = OltFirmwareImage(
        vendor="Huawei",
        model="MA5800-X7",
        version="V1R2",
        file_url="sftp://firmware.example/ma5800-v1r2.bin",
        filename="ma5800-v1r2.bin",
        checksum="sha256:abc",
        upgrade_method="sftp",
        is_active=True,
    )
    db_session.add_all([olt, image])
    db_session.commit()
    return olt, image


def test_request_stages_operation_without_claiming_device_success(
    db_session, monkeypatch
) -> None:
    olt, image = _inventory(db_session)
    monkeypatch.setattr(
        olt_firmware,
        "enqueue_task",
        lambda *args, **kwargs: QueueDispatchResult(
            queued=True, task_id="task-1", task_name=args[0]
        ),
    )

    result = request_firmware_upgrade(
        db_session, str(olt.id), str(image.id), initiated_by="operator"
    )

    assert result.success is True
    assert result.waiting is True
    operation = network_operations.get(db_session, result.data["operation_id"])
    assert operation.operation_type == NetworkOperationType.olt_firmware_upgrade
    assert operation.target_type == NetworkOperationTargetType.olt
    assert operation.status == NetworkOperationStatus.pending
    assert operation.input_payload["target_version"] == "V1R2"
    assert operation.initiated_by == "operator"
    db_session.refresh(olt)
    assert olt.firmware_version == "V1R1"


def test_request_rejects_cross_model_image(db_session, monkeypatch) -> None:
    olt, image = _inventory(db_session)
    image.model = "MA5608T"
    db_session.commit()
    queued = False

    def _enqueue(*args, **kwargs):
        nonlocal queued
        queued = True
        return QueueDispatchResult(queued=True)

    monkeypatch.setattr(olt_firmware, "enqueue_task", _enqueue)

    result = request_firmware_upgrade(db_session, str(olt.id), str(image.id))

    assert result.success is False
    assert "does not match" in result.message
    assert queued is False


def test_upgrade_requires_exact_target_readback(db_session, monkeypatch) -> None:
    olt, image = _inventory(db_session)
    firmware_reads = iter(
        [
            (True, "running V1R1", FirmwareInfo(current_version="V1R1")),
            (True, "running V1R3", FirmwareInfo(current_version="V1R3")),
        ]
    )
    monkeypatch.setattr(
        olt_firmware.olt_ssh_service,
        "get_firmware_info",
        lambda _olt: next(firmware_reads),
    )
    monkeypatch.setattr(
        olt_firmware.olt_ssh_service,
        "upgrade_firmware",
        lambda *_args, **_kwargs: (True, "accepted"),
    )
    monkeypatch.setattr(
        olt_firmware,
        "poll_olt_reachability",
        lambda *_args, **_kwargs: (True, "reachable"),
    )
    waiting: list[str] = []

    result = upgrade_with_verification(
        db_session,
        str(olt.id),
        str(image.id),
        initial_wait_sec=0,
        on_waiting=waiting.append,
    )

    assert result.success is False
    assert result.verified_version is None
    assert "expected V1R2, got V1R3" in result.message
    assert waiting and "waiting for OLT reboot" in waiting[0]


def test_task_commits_readback_and_success_atomically(db_session, monkeypatch) -> None:
    from app.tasks.olt_firmware import upgrade_firmware_task

    olt, image = _inventory(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_firmware_upgrade,
        NetworkOperationTargetType.olt,
        str(olt.id),
        correlation_key=f"olt_firmware_upgrade:{olt.id}",
        input_payload={
            "firmware_image_id": str(image.id),
            "target_version": image.version,
        },
    )
    db_session.commit()
    olt_id = str(olt.id)
    image_id = str(image.id)
    operation_id = str(operation.id)

    def _verified(*args, **kwargs):
        kwargs["on_waiting"]("waiting for reboot")
        return FirmwareUpgradeResult(
            success=True,
            message="verified",
            current_version="V1R1",
            target_version="V1R2",
            verified_version="V1R2",
            reachable_after=True,
        )

    monkeypatch.setattr(
        "app.tasks.olt_firmware.db_session_adapter.create_session",
        lambda: db_session,
    )
    monkeypatch.setattr(
        "app.services.network.olt_firmware.upgrade_with_verification_audited",
        _verified,
    )
    task_body = inspect.unwrap(upgrade_firmware_task.run)

    result = task_body(
        olt_id,
        image_id,
        operation_id=operation_id,
        initial_wait_sec=0,
    )

    assert result["verified"] is True
    db_session.expire_all()
    operation = network_operations.get(db_session, operation_id)
    assert operation.status == NetworkOperationStatus.succeeded
    assert operation.output_payload["verified"] is True
    assert db_session.get(OLTDevice, olt_id).firmware_version == "V1R2"


def test_task_rejects_arguments_outside_operation_scope(
    db_session, monkeypatch
) -> None:
    from app.tasks.olt_firmware import upgrade_firmware_task

    olt, image = _inventory(db_session)
    operation = network_operations.start(
        db_session,
        NetworkOperationType.olt_firmware_upgrade,
        NetworkOperationTargetType.olt,
        str(olt.id),
        correlation_key=f"olt_firmware_upgrade:{olt.id}",
        input_payload={
            "firmware_image_id": str(image.id),
            "target_version": image.version,
        },
    )
    db_session.commit()
    operation_id = str(operation.id)
    monkeypatch.setattr(
        "app.tasks.olt_firmware.db_session_adapter.create_session",
        lambda: db_session,
    )
    task_body = inspect.unwrap(upgrade_firmware_task.run)

    result = task_body(
        str(olt.id),
        "00000000-0000-0000-0000-000000000001",
        operation_id=operation_id,
    )

    assert result["success"] is False
    db_session.expire_all()
    operation = network_operations.get(db_session, operation_id)
    assert operation.status == NetworkOperationStatus.failed
    assert "do not match" in operation.error
