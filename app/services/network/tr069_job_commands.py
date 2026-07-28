"""Typed admission and lifecycle coordination for TR-069 CPE commands.

The network operation ledger owns operation state and the durable dispatch
outbox owns broker delivery. This coordinator admits a closed TR-069 command,
creates the operator-facing ``Tr069Job`` projection in the same transaction,
and records normalized ACS delivery observations. Capability controls apply
only to new admission; accepted work never consults them again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Job,
    Tr069JobStatus,
)
from app.services import control_registry
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network_operation_dispatch import (
    NetworkOperationCommand,
    stage_dispatch,
)
from app.services.network_operations import network_operations
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

Tr069Scalar: TypeAlias = str | bool | int

_ADMISSION = OwnerCommandDefinition(
    owner="network.tr069_commands",
    concern="TR-069 command admission coordination",
    name="request_tr069_command",
)
_EXECUTION_CLAIM = OwnerCommandDefinition(
    owner="network.tr069_commands",
    concern="TR-069 command execution coordination",
    name="claim_tr069_command_execution",
)
_OUTCOME = OwnerCommandDefinition(
    owner="network.tr069_commands",
    concern="TR-069 command outcome coordination",
    name="record_tr069_command_observation",
)
_ACTIVE_OPERATION_STATUSES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
    NetworkOperationStatus.waiting,
)
_SAFE_REFRESH_ROOTS: dict[str, tuple[str, ...]] = {
    "Device.": (
        "Device.DeviceInfo.",
        "Device.ManagementServer.",
        "Device.WiFi.",
        "Device.IP.",
        "Device.Hosts.",
        "Device.Ethernet.",
        "Device.PPP.",
    ),
    "InternetGatewayDevice.": (
        "InternetGatewayDevice.DeviceInfo.",
        "InternetGatewayDevice.ManagementServer.",
        "InternetGatewayDevice.WANDevice.1.",
        "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.",
        "InternetGatewayDevice.LANDevice.1.Hosts.",
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.",
        "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.",
    ),
}
_ALLOWED_VALUE_TYPES = frozenset(
    {
        "xsd:boolean",
        "xsd:int",
        "xsd:integer",
        "xsd:string",
        "xsd:unsignedInt",
    }
)


class Tr069CommandKind(StrEnum):
    refresh_object = "refreshObject"
    reboot = "reboot"
    factory_reset = "factoryReset"
    set_parameter_values = "setParameterValues"
    download = "download"


class Tr069DeliveryState(StrEnum):
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    unverified = "unverified"


@dataclass(frozen=True)
class Tr069ParameterValue:
    path: str
    value: Tr069Scalar
    value_type: str


@dataclass(frozen=True)
class Tr069Download:
    url: str
    filename: str | None = None
    file_type: str = "1 Firmware Upgrade Image"


@dataclass(frozen=True)
class Tr069CommandRequest:
    context: CommandContext
    device_id: UUID
    name: str
    kind: Tr069CommandKind
    object_name: str | None = None
    parameter_values: tuple[Tr069ParameterValue, ...] = ()
    download: Tr069Download | None = None


@dataclass(frozen=True)
class Tr069AdmissionOutcome:
    accepted: bool
    duplicate: bool
    job_id: UUID
    operation_id: UUID
    dispatch_id: UUID
    status: Tr069JobStatus
    name: str
    command: Tr069CommandKind


@dataclass(frozen=True)
class Tr069ExecutionPlan:
    job_id: UUID
    operation_id: UUID
    server_url: str
    genieacs_device_id: str
    kind: Tr069CommandKind
    object_names: tuple[str, ...] = ()
    parameter_values: tuple[Tr069ParameterValue, ...] = ()
    download: Tr069Download | None = None


@dataclass(frozen=True)
class Tr069ExecutionClaim:
    executable: bool
    plan: Tr069ExecutionPlan | None
    status: Tr069JobStatus
    reason: str | None = None


@dataclass(frozen=True)
class Tr069DeliveryObservation:
    context: CommandContext
    job_id: UUID
    operation_id: UUID
    state: Tr069DeliveryState
    external_task_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class Tr069LifecycleOutcome:
    job_id: UUID
    operation_id: UUID
    status: Tr069JobStatus


class Tr069CommandError(DomainError):
    """Stable, transport-neutral TR-069 command rejection."""


def _error(code: str, message: str, **details: object) -> Tr069CommandError:
    return Tr069CommandError(
        code=f"network.tr069_commands.{code}",
        message=message,
        details=details,
    )


def _validate_parameter(item: Tr069ParameterValue) -> None:
    path = item.path.strip()
    if not path or len(path) > 512:
        raise _error(
            "invalid_parameter",
            "TR-069 parameter paths must be between 1 and 512 characters.",
        )
    if item.value_type not in _ALLOWED_VALUE_TYPES:
        raise _error(
            "invalid_parameter_type",
            "TR-069 parameter value type is not supported.",
            path=path,
        )
    if (
        (item.value_type == "xsd:boolean" and not isinstance(item.value, bool))
        or (
            item.value_type in {"xsd:int", "xsd:integer", "xsd:unsignedInt"}
            and (not isinstance(item.value, int) or isinstance(item.value, bool))
        )
        or (item.value_type == "xsd:string" and not isinstance(item.value, str))
    ):
        raise _error(
            "invalid_parameter_value",
            "TR-069 parameter value does not match its declared type.",
            path=path,
        )
    if (
        item.value_type == "xsd:unsignedInt"
        and isinstance(item.value, int)
        and not isinstance(item.value, bool)
        and item.value < 0
    ):
        raise _error(
            "invalid_parameter_value",
            "TR-069 unsigned parameter value cannot be negative.",
            path=path,
        )
    if isinstance(item.value, str) and len(item.value) > 4096:
        raise _error(
            "invalid_parameter",
            "TR-069 parameter values cannot exceed 4096 characters.",
            path=path,
        )


def _validate_download(download: Tr069Download) -> None:
    parsed = urlsplit(download.url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _error(
            "invalid_download",
            "Firmware download URL must use HTTP or HTTPS.",
        )
    if parsed.username or parsed.password:
        raise _error(
            "invalid_download",
            "Firmware download URL cannot contain embedded credentials.",
        )
    if len(download.url) > 2048:
        raise _error("invalid_download", "Firmware download URL is too long.")
    if download.filename is not None and len(download.filename.strip()) > 255:
        raise _error("invalid_download", "Firmware filename is too long.")


def _validate_request(request: Tr069CommandRequest) -> None:
    if not request.name.strip() or len(request.name.strip()) > 160:
        raise _error(
            "invalid_command",
            "TR-069 command name must be between 1 and 160 characters.",
        )
    if request.kind is Tr069CommandKind.refresh_object:
        object_name = str(request.object_name or "").strip()
        if object_name not in _SAFE_REFRESH_ROOTS and not (
            object_name.startswith(("Device.", "InternetGatewayDevice."))
            and object_name.endswith(".")
            and len(object_name) <= 512
        ):
            raise _error(
                "invalid_refresh_root",
                "TR-069 refresh object is outside the supported data model.",
            )
        if request.parameter_values or request.download is not None:
            raise _error("invalid_command", "Refresh commands contain extra payload.")
        return
    if request.kind is Tr069CommandKind.set_parameter_values:
        if not request.parameter_values or len(request.parameter_values) > 100:
            raise _error(
                "invalid_parameter",
                "TR-069 configuration requires between 1 and 100 parameters.",
            )
        for item in request.parameter_values:
            _validate_parameter(item)
        if request.object_name is not None or request.download is not None:
            raise _error(
                "invalid_command",
                "TR-069 configuration contains incompatible payload.",
            )
        return
    if request.kind is Tr069CommandKind.download:
        if request.download is None:
            raise _error("invalid_download", "Firmware download payload is required.")
        _validate_download(request.download)
        if request.object_name is not None or request.parameter_values:
            raise _error(
                "invalid_command",
                "Firmware download contains incompatible payload.",
            )
        return
    if (
        request.object_name is not None
        or request.parameter_values
        or request.download is not None
    ):
        raise _error(
            "invalid_command",
            "This TR-069 command does not accept a payload.",
        )


def _secure_payload(request: Tr069CommandRequest) -> dict[str, object]:
    payload: dict[str, object] = {}
    if request.kind is Tr069CommandKind.refresh_object:
        object_name = str(request.object_name or "").strip()
        payload["object_names"] = list(
            _SAFE_REFRESH_ROOTS.get(object_name, (object_name,))
        )
    elif request.kind is Tr069CommandKind.set_parameter_values:
        payload["parameter_values"] = [
            {
                "path": item.path.strip(),
                "value": item.value,
                "value_type": item.value_type,
            }
            for item in request.parameter_values
        ]
    elif request.kind is Tr069CommandKind.download and request.download is not None:
        payload["download"] = {
            "url": request.download.url.strip(),
            "filename": (
                request.download.filename.strip() if request.download.filename else None
            ),
            "file_type": request.download.file_type,
        }
    return payload


def _public_payload(request: Tr069CommandRequest) -> dict[str, object] | None:
    if request.kind is Tr069CommandKind.refresh_object:
        return {"object_name": str(request.object_name or "").strip()}
    if request.kind is Tr069CommandKind.set_parameter_values:
        return {
            "parameter_count": len(request.parameter_values),
            "parameter_paths": [item.path.strip() for item in request.parameter_values],
            "values": "[redacted]",
        }
    if request.kind is Tr069CommandKind.download:
        return {
            "file_type": (
                request.download.file_type if request.download is not None else None
            ),
            "filename": (
                request.download.filename if request.download is not None else None
            ),
            "url": "[redacted]",
        }
    return None


def _fingerprint(
    *, device_id: UUID, kind: Tr069CommandKind, secure_payload: dict[str, object]
) -> str:
    material = json.dumps(
        {
            "device_id": str(device_id),
            "command": kind.value,
            "payload": secure_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _active_operation(db: Session, correlation_key: str) -> NetworkOperation | None:
    return db.scalars(
        select(NetworkOperation)
        .where(
            NetworkOperation.correlation_key == correlation_key,
            NetworkOperation.status.in_(_ACTIVE_OPERATION_STATUSES),
        )
        .order_by(NetworkOperation.created_at.desc())
        .limit(1)
    ).first()


def _active_device_operation(
    db: Session,
    device_id: UUID,
) -> NetworkOperation | None:
    return db.scalars(
        select(NetworkOperation)
        .where(
            NetworkOperation.operation_type == NetworkOperationType.cpe_tr069_command,
            NetworkOperation.target_type == NetworkOperationTargetType.cpe,
            NetworkOperation.target_id == device_id,
            NetworkOperation.status.in_(_ACTIVE_OPERATION_STATUSES),
        )
        .order_by(NetworkOperation.created_at.asc())
        .limit(1)
    ).first()


def _latest_device_operation(
    db: Session,
    device_id: UUID,
) -> NetworkOperation | None:
    return db.scalars(
        select(NetworkOperation)
        .where(
            NetworkOperation.operation_type == NetworkOperationType.cpe_tr069_command,
            NetworkOperation.target_type == NetworkOperationTargetType.cpe,
            NetworkOperation.target_id == device_id,
        )
        .order_by(NetworkOperation.created_at.desc())
        .limit(1)
    ).first()


def _outcome_for_operation(
    db: Session,
    operation: NetworkOperation,
    *,
    duplicate: bool,
) -> Tr069AdmissionOutcome:
    job = db.scalars(
        select(Tr069Job).where(Tr069Job.network_operation_id == operation.id)
    ).one_or_none()
    dispatch = db.scalars(
        select(NetworkOperationDispatch)
        .where(NetworkOperationDispatch.operation_id == operation.id)
        .order_by(NetworkOperationDispatch.created_at.asc())
        .limit(1)
    ).one_or_none()
    if job is None or dispatch is None:
        raise _error(
            "operation_projection_missing",
            "Existing TR-069 operation is missing its durable projection.",
            operation_id=str(operation.id),
        )
    return Tr069AdmissionOutcome(
        accepted=True,
        duplicate=duplicate,
        job_id=job.id,
        operation_id=operation.id,
        dispatch_id=dispatch.id,
        status=job.status,
        name=job.name,
        command=Tr069CommandKind(job.command),
    )


def _emit_job_event(
    db: Session,
    *,
    event_type: EventType,
    job: Tr069Job,
    actor: str,
) -> None:
    emit_event(
        db,
        event_type,
        {
            "job_id": str(job.id),
            "device_id": str(job.device_id),
            "operation_id": str(job.network_operation_id),
            "command": job.command,
            "status": job.status.value,
            "error": job.error,
        },
        actor=actor,
    )


def _admit(
    db: Session,
    request: Tr069CommandRequest,
) -> Tr069AdmissionOutcome:
    _validate_request(request)
    if not control_registry.is_enabled(db, "network.tr069_command_admission"):
        raise _error(
            "admission_disabled",
            "New TR-069 commands are currently disabled.",
        )

    device = db.scalar(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.id == request.device_id)
        .with_for_update()
    )
    if device is None:
        raise _error("device_not_found", "TR-069 device not found.")
    if not device.is_active:
        raise _error("device_inactive", "TR-069 device is inactive.")
    if not str(device.genieacs_device_id or "").strip():
        raise _error(
            "device_not_registered",
            "TR-069 device has not registered in GenieACS.",
        )
    server = db.get(Tr069AcsServer, device.acs_server_id)
    if server is None or not server.is_active or not str(server.base_url or "").strip():
        raise _error(
            "acs_unavailable",
            "TR-069 device has no active ACS server.",
        )

    protected = _secure_payload(request)
    fingerprint = _fingerprint(
        device_id=request.device_id,
        kind=request.kind,
        secure_payload=protected,
    )
    correlation_key = f"tr069:{request.device_id}:{fingerprint}"
    active = _active_operation(db, correlation_key)
    if active is not None:
        return _outcome_for_operation(db, active, duplicate=True)
    active_for_device = _active_device_operation(db, request.device_id)
    if active_for_device is not None:
        raise _error(
            "device_command_in_progress",
            "Another TR-069 command is already active for this device.",
            operation_id=str(active_for_device.id),
        )
    latest_for_device = _latest_device_operation(db, request.device_id)
    if (
        latest_for_device is not None
        and latest_for_device.status is NetworkOperationStatus.warning
    ):
        completed_at = latest_for_device.completed_at
        last_inform_at = device.last_inform_at
        if completed_at is None or last_inform_at is None:
            fresh_after_ambiguity = False
        else:
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=UTC)
            if last_inform_at.tzinfo is None:
                last_inform_at = last_inform_at.replace(tzinfo=UTC)
            fresh_after_ambiguity = last_inform_at > completed_at
        if not fresh_after_ambiguity:
            raise _error(
                "device_state_review_required",
                "A fresh device Inform is required after an unverified command.",
                operation_id=str(latest_for_device.id),
            )

    job = Tr069Job(
        id=uuid4(),
        device_id=request.device_id,
        name=request.name.strip(),
        command=request.kind.value,
        payload=_public_payload(request),
        secure_payload=protected or None,
        status=Tr069JobStatus.queued,
    )
    db.add(job)
    db.flush()

    operation = network_operations.start(
        db,
        NetworkOperationType.cpe_tr069_command,
        NetworkOperationTargetType.cpe,
        str(request.device_id),
        correlation_key=correlation_key,
        input_payload={
            "job_id": str(job.id),
            "device_id": str(request.device_id),
            "command": request.kind.value,
            "command_fingerprint": fingerprint,
        },
        initiated_by=request.context.actor,
    )
    job.network_operation_id = operation.id
    dispatch = stage_dispatch(
        db,
        operation,
        NetworkOperationCommand.cpe_tr069_command_v1,
        max_attempts=5,
    )
    _emit_job_event(
        db,
        event_type=EventType.tr069_job_accepted,
        job=job,
        actor=request.context.actor,
    )
    db.flush()
    return Tr069AdmissionOutcome(
        accepted=True,
        duplicate=False,
        job_id=job.id,
        operation_id=operation.id,
        dispatch_id=dispatch.id,
        status=job.status,
        name=job.name,
        command=request.kind,
    )


def request_tr069_command(
    db: Session,
    request: Tr069CommandRequest,
) -> Tr069AdmissionOutcome:
    """Atomically admit one command, operation, dispatch, and job projection."""

    try:
        return execute_owner_command(
            db,
            definition=_ADMISSION,
            context=request.context,
            operation=lambda: _admit(db, request),
        )
    except IntegrityError as exc:
        raise _error(
            "concurrent_admission",
            "A matching TR-069 command was admitted concurrently; retry to replay it.",
        ) from exc


def _decode_secure_plan(
    job: Tr069Job,
) -> tuple[
    tuple[str, ...],
    tuple[Tr069ParameterValue, ...],
    Tr069Download | None,
]:
    payload = job.secure_payload or {}
    object_names = tuple(str(item) for item in payload.get("object_names", []))
    values: list[Tr069ParameterValue] = []
    for item in payload.get("parameter_values", []):
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, (str, bool, int)):
            continue
        values.append(
            Tr069ParameterValue(
                path=str(item.get("path") or ""),
                value=value,
                value_type=str(item.get("value_type") or ""),
            )
        )
    raw_download = payload.get("download")
    download = (
        Tr069Download(
            url=str(raw_download.get("url") or ""),
            filename=str(raw_download.get("filename") or "") or None,
            file_type=str(raw_download.get("file_type") or "1 Firmware Upgrade Image"),
        )
        if isinstance(raw_download, dict)
        else None
    )
    return object_names, tuple(values), download


def claim_tr069_command_execution(
    db: Session,
    *,
    operation_id: UUID,
    job_id: UUID,
    context: CommandContext,
) -> Tr069ExecutionClaim:
    """Claim the domain transition after dispatch has claimed worker delivery."""

    def _claim() -> Tr069ExecutionClaim:
        operation = db.scalar(
            select(NetworkOperation)
            .where(NetworkOperation.id == operation_id)
            .with_for_update()
        )
        job = db.scalar(select(Tr069Job).where(Tr069Job.id == job_id).with_for_update())
        if (
            operation is None
            or job is None
            or job.network_operation_id != operation.id
            or operation.operation_type != NetworkOperationType.cpe_tr069_command
        ):
            raise _error(
                "operation_projection_missing",
                "TR-069 operation and job projection do not match.",
            )
        if operation.status not in _ACTIVE_OPERATION_STATUSES:
            return Tr069ExecutionClaim(
                executable=False,
                plan=None,
                status=job.status,
                reason="operation_terminal",
            )
        if job.status is not Tr069JobStatus.queued:
            return Tr069ExecutionClaim(
                executable=False,
                plan=None,
                status=job.status,
                reason="job_not_queued",
            )
        device = db.get(Tr069CpeDevice, job.device_id)
        server = db.get(Tr069AcsServer, device.acs_server_id) if device else None
        genieacs_device_id = (
            str(device.genieacs_device_id or "").strip() if device else ""
        )
        server_url = str(server.base_url or "").strip() if server else ""
        if (
            device is None
            or not device.is_active
            or server is None
            or not server.is_active
            or not genieacs_device_id
            or not server_url
        ):
            reason = "TR-069 execution prerequisites are no longer available."
            network_operations.mark_failed(db, str(operation.id), reason)
            job.status = Tr069JobStatus.failed
            job.error = reason
            job.completed_at = datetime.now(UTC)
            job.last_observed_at = datetime.now(UTC)
            job.secure_payload = None
            _emit_job_event(
                db,
                event_type=EventType.tr069_job_failed,
                job=job,
                actor=context.actor,
            )
            db.flush()
            return Tr069ExecutionClaim(
                executable=False,
                plan=None,
                status=job.status,
                reason=reason,
            )
        try:
            kind = Tr069CommandKind(job.command)
        except ValueError as exc:
            raise _error(
                "invalid_command",
                "Stored TR-069 command is not supported.",
                job_id=str(job.id),
            ) from exc
        object_names, parameter_values, download = _decode_secure_plan(job)
        network_operations.mark_running(db, str(operation.id))
        job.status = Tr069JobStatus.running
        job.started_at = datetime.now(UTC)
        job.completed_at = None
        job.error = None
        job.last_observed_at = datetime.now(UTC)
        db.flush()
        return Tr069ExecutionClaim(
            executable=True,
            plan=Tr069ExecutionPlan(
                job_id=job.id,
                operation_id=operation.id,
                server_url=server_url,
                genieacs_device_id=genieacs_device_id,
                kind=kind,
                object_names=object_names,
                parameter_values=parameter_values,
                download=download,
            ),
            status=job.status,
        )

    return execute_owner_command(
        db,
        definition=_EXECUTION_CLAIM,
        context=context,
        operation=_claim,
    )


def record_tr069_command_observation(
    db: Session,
    observation: Tr069DeliveryObservation,
) -> Tr069LifecycleOutcome:
    """Project one normalized ACS delivery observation into both ledgers."""

    def _record() -> Tr069LifecycleOutcome:
        operation = db.scalar(
            select(NetworkOperation)
            .where(NetworkOperation.id == observation.operation_id)
            .with_for_update()
        )
        job = db.scalar(
            select(Tr069Job).where(Tr069Job.id == observation.job_id).with_for_update()
        )
        if operation is None or job is None or job.network_operation_id != operation.id:
            raise _error(
                "operation_projection_missing",
                "TR-069 operation and job projection do not match.",
            )
        if operation.status not in _ACTIVE_OPERATION_STATUSES:
            if (
                job.status
                in {
                    Tr069JobStatus.queued,
                    Tr069JobStatus.running,
                    Tr069JobStatus.pending,
                }
                and observation.state is Tr069DeliveryState.unverified
            ):
                now = datetime.now(UTC)
                job.status = Tr069JobStatus.unverified
                job.error = (
                    str(observation.reason or "").strip()
                    or "Terminal operation has ambiguous ACS delivery evidence."
                )
                job.external_task_ids = list(
                    dict.fromkeys(observation.external_task_ids)
                )
                job.last_observed_at = now
                job.completed_at = now
                job.secure_payload = None
                _emit_job_event(
                    db,
                    event_type=EventType.tr069_job_unverified,
                    job=job,
                    actor=observation.context.actor,
                )
                db.flush()
            return Tr069LifecycleOutcome(
                job_id=job.id,
                operation_id=operation.id,
                status=job.status,
            )
        if job.status not in {Tr069JobStatus.running, Tr069JobStatus.pending}:
            raise _error(
                "invalid_observation",
                "TR-069 delivery evidence requires a claimed command.",
                status=job.status.value,
            )

        now = datetime.now(UTC)
        reason = str(observation.reason or "").strip() or None
        job.external_task_ids = list(dict.fromkeys(observation.external_task_ids))
        job.last_observed_at = now
        if observation.state is Tr069DeliveryState.waiting:
            network_operations.mark_waiting(
                db,
                str(operation.id),
                reason or "Waiting for GenieACS task completion.",
            )
            job.status = Tr069JobStatus.pending
            job.submitted_at = job.submitted_at or now
            job.error = reason
            job.completed_at = None
        elif observation.state is Tr069DeliveryState.succeeded:
            network_operations.mark_succeeded(
                db,
                str(operation.id),
                output_payload={
                    "job_id": str(job.id),
                    "external_task_ids": list(observation.external_task_ids),
                    "evidence": (
                        "genieacs_task_absent_without_fault_after_fresh_inform"
                    ),
                },
            )
            job.status = Tr069JobStatus.succeeded
            job.error = None
            job.completed_at = now
            job.secure_payload = None
            _emit_job_event(
                db,
                event_type=EventType.tr069_job_completed,
                job=job,
                actor=observation.context.actor,
            )
        elif observation.state is Tr069DeliveryState.failed:
            failure = reason or "GenieACS reported command failure."
            network_operations.mark_failed(db, str(operation.id), failure)
            job.status = Tr069JobStatus.failed
            job.error = failure
            job.completed_at = now
            job.secure_payload = None
            _emit_job_event(
                db,
                event_type=EventType.tr069_job_failed,
                job=job,
                actor=observation.context.actor,
            )
        else:
            warning = reason or (
                "ACS command outcome could not be verified; review current device "
                "state before retrying."
            )
            network_operations.mark_warning(
                db,
                str(operation.id),
                warning,
                output_payload={
                    "job_id": str(job.id),
                    "external_task_ids": list(observation.external_task_ids),
                    "evidence": "delivery_unverified",
                },
            )
            job.status = Tr069JobStatus.unverified
            job.error = warning
            job.completed_at = now
            job.secure_payload = None
            _emit_job_event(
                db,
                event_type=EventType.tr069_job_unverified,
                job=job,
                actor=observation.context.actor,
            )
        db.flush()
        return Tr069LifecycleOutcome(
            job_id=job.id,
            operation_id=operation.id,
            status=job.status,
        )

    return execute_owner_command(
        db,
        definition=_OUTCOME,
        context=observation.context,
        operation=_record,
    )
