from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationDispatch,
    NetworkOperationStatus,
)
from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Job,
    Tr069JobStatus,
)
from app.services.network.tr069_job_commands import (
    Tr069CommandError,
    Tr069CommandKind,
    Tr069CommandRequest,
    Tr069DeliveryObservation,
    Tr069DeliveryState,
    Tr069Download,
    Tr069ParameterValue,
    claim_tr069_command_execution,
    record_tr069_command_observation,
    request_tr069_command,
)
from app.services.network_operations import network_operations
from app.services.owner_commands import CommandContext
from app.tasks.tr069 import _submit_tr069_plan, _Tr069SubmissionBlocked


def _context(device_id, phase: str) -> CommandContext:
    return CommandContext.system(
        actor="test:network-admin",
        scope=f"network:tr069:{device_id}",
        reason=f"TR-069 lifecycle test {phase}",
        command_id=uuid4(),
    )


def _device(db_session) -> UUID:
    server = Tr069AcsServer(
        name=f"Command ACS {uuid4()}",
        base_url="https://acs.test.invalid",
        is_active=True,
    )
    db_session.add(server)
    db_session.flush()
    device = Tr069CpeDevice(
        acs_server_id=server.id,
        serial_number=f"TR069-COMMAND-{uuid4()}",
        genieacs_device_id=f"genieacs-{uuid4()}",
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()
    device_id = device.id
    db_session.commit()
    return device_id


def _reboot_request(device_id: UUID, phase: str) -> Tr069CommandRequest:
    return Tr069CommandRequest(
        context=_context(device_id, phase),
        device_id=device_id,
        name="Reboot Device",
        kind=Tr069CommandKind.reboot,
    )


def test_admission_atomically_stages_operation_dispatch_and_redacted_projection(
    db_session,
):
    device_id = _device(db_session)

    outcome = request_tr069_command(
        db_session,
        Tr069CommandRequest(
            context=_context(device_id, "admit"),
            device_id=device_id,
            name="Set WiFi Password",
            kind=Tr069CommandKind.set_parameter_values,
            parameter_values=(
                Tr069ParameterValue(
                    path="Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
                    value="not-a-real-password",
                    value_type="xsd:string",
                ),
            ),
        ),
    )

    operation = db_session.get(NetworkOperation, outcome.operation_id)
    dispatch = db_session.get(NetworkOperationDispatch, outcome.dispatch_id)
    job = db_session.get(Tr069Job, outcome.job_id)
    assert operation is not None
    assert dispatch is not None
    assert job is not None
    assert operation.status is NetworkOperationStatus.pending
    assert dispatch.command_name == "cpe_tr069_command.v1"
    assert dispatch.task_name == "app.tasks.tr069.execute_network_operation_job"
    assert dispatch.args_payload == [str(operation.id), str(job.id)]
    assert job.network_operation_id == operation.id
    assert job.status is Tr069JobStatus.queued
    assert job.payload == {
        "parameter_count": 1,
        "parameter_paths": ["Device.WiFi.AccessPoint.1.Security.KeyPassphrase"],
        "values": "[redacted]",
    }

    raw_payload = db_session.execute(
        text("SELECT secure_payload FROM tr069_jobs WHERE id = :job_id"),
        {"job_id": str(job.id)},
    ).scalar_one()
    assert "not-a-real-password" not in str(raw_payload)


def test_active_duplicate_replays_one_operation_and_dispatch(db_session):
    device_id = _device(db_session)

    first = request_tr069_command(db_session, _reboot_request(device_id, "first"))
    replay = request_tr069_command(db_session, _reboot_request(device_id, "replay"))

    assert replay.duplicate is True
    assert replay.job_id == first.job_id
    assert replay.operation_id == first.operation_id
    assert replay.dispatch_id == first.dispatch_id
    assert (
        db_session.query(Tr069Job).filter(Tr069Job.device_id == device_id).count() == 1
    )


def test_different_command_is_rejected_while_device_command_is_active(db_session):
    device_id = _device(db_session)
    request_tr069_command(db_session, _reboot_request(device_id, "active-reboot"))

    with pytest.raises(Tr069CommandError) as exc_info:
        request_tr069_command(
            db_session,
            Tr069CommandRequest(
                context=_context(device_id, "conflicting-refresh"),
                device_id=device_id,
                name="Refresh Parameters",
                kind=Tr069CommandKind.refresh_object,
                object_name="Device.",
            ),
        )

    assert exc_info.value.code == ("network.tr069_commands.device_command_in_progress")


def test_disabled_admission_creates_no_lifecycle_rows(db_session, monkeypatch):
    device_id = _device(db_session)
    monkeypatch.setattr(
        "app.services.network.tr069_job_commands.control_registry.is_enabled",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(Tr069CommandError) as exc_info:
        request_tr069_command(db_session, _reboot_request(device_id, "disabled"))

    assert exc_info.value.code == "network.tr069_commands.admission_disabled"
    assert (
        db_session.scalars(
            select(Tr069Job).where(Tr069Job.device_id == device_id)
        ).all()
        == []
    )


def test_accepted_command_claim_ignores_later_admission_disable(
    db_session,
    monkeypatch,
):
    device_id = _device(db_session)
    admitted = request_tr069_command(
        db_session,
        _reboot_request(device_id, "admit-before-disable"),
    )
    monkeypatch.setattr(
        "app.services.network.tr069_job_commands.control_registry.is_enabled",
        lambda *_args, **_kwargs: False,
    )

    claim = claim_tr069_command_execution(
        db_session,
        operation_id=admitted.operation_id,
        job_id=admitted.job_id,
        context=_context(device_id, "claim-after-disable"),
    )

    assert claim.executable is True
    assert claim.plan is not None
    assert claim.plan.kind is Tr069CommandKind.reboot


def test_missing_execution_prerequisite_terminalizes_both_ledgers(db_session):
    device_id = _device(db_session)
    admitted = request_tr069_command(
        db_session,
        _reboot_request(device_id, "missing-prerequisite"),
    )
    device = db_session.get(Tr069CpeDevice, device_id)
    assert device is not None
    device.is_active = False
    db_session.commit()

    claim = claim_tr069_command_execution(
        db_session,
        operation_id=admitted.operation_id,
        job_id=admitted.job_id,
        context=_context(device_id, "claim"),
    )

    operation = db_session.get(NetworkOperation, admitted.operation_id)
    job = db_session.get(Tr069Job, admitted.job_id)
    assert claim.executable is False
    assert operation is not None and operation.status is NetworkOperationStatus.failed
    assert job is not None and job.status is Tr069JobStatus.failed
    assert job.completed_at is not None


def test_delivery_observation_requires_a_claimed_command(db_session):
    device_id = _device(db_session)
    admitted = request_tr069_command(
        db_session,
        _reboot_request(device_id, "unclaimed-admit"),
    )

    with pytest.raises(Tr069CommandError) as exc_info:
        record_tr069_command_observation(
            db_session,
            Tr069DeliveryObservation(
                context=_context(device_id, "unclaimed-observation"),
                job_id=admitted.job_id,
                operation_id=admitted.operation_id,
                state=Tr069DeliveryState.succeeded,
            ),
        )

    assert exc_info.value.code == "network.tr069_commands.invalid_observation"
    job = db_session.get(Tr069Job, admitted.job_id)
    assert job is not None and job.status is Tr069JobStatus.queued


def test_pending_then_success_moves_both_ledgers_and_clears_secret(db_session):
    device_id = _device(db_session)
    admitted = request_tr069_command(
        db_session,
        Tr069CommandRequest(
            context=_context(device_id, "firmware-admit"),
            device_id=device_id,
            name="Firmware Update",
            kind=Tr069CommandKind.download,
            download=Tr069Download(
                url="https://firmware.test.invalid/image.bin",
                filename="image.bin",
            ),
        ),
    )
    claim_tr069_command_execution(
        db_session,
        operation_id=admitted.operation_id,
        job_id=admitted.job_id,
        context=_context(device_id, "firmware-claim"),
    )

    waiting = record_tr069_command_observation(
        db_session,
        Tr069DeliveryObservation(
            context=_context(device_id, "waiting"),
            job_id=admitted.job_id,
            operation_id=admitted.operation_id,
            state=Tr069DeliveryState.waiting,
            external_task_ids=("acs-task-1",),
        ),
    )
    assert waiting.status is Tr069JobStatus.pending
    waiting_job = db_session.get(Tr069Job, admitted.job_id)
    assert waiting_job is not None
    assert waiting_job.submitted_at is not None
    db_session.commit()

    succeeded = record_tr069_command_observation(
        db_session,
        Tr069DeliveryObservation(
            context=_context(device_id, "succeeded"),
            job_id=admitted.job_id,
            operation_id=admitted.operation_id,
            state=Tr069DeliveryState.succeeded,
            external_task_ids=("acs-task-1",),
        ),
    )

    operation = db_session.get(NetworkOperation, admitted.operation_id)
    job = db_session.get(Tr069Job, admitted.job_id)
    assert succeeded.status is Tr069JobStatus.succeeded
    assert operation is not None
    assert operation.status is NetworkOperationStatus.succeeded
    assert job is not None
    assert job.secure_payload is None
    assert job.external_task_ids == ["acs-task-1"]


def test_ambiguous_delivery_is_terminal_unverified_and_not_replayable(db_session):
    device_id = _device(db_session)
    admitted = request_tr069_command(
        db_session,
        _reboot_request(device_id, "ambiguous-admit"),
    )
    claim_tr069_command_execution(
        db_session,
        operation_id=admitted.operation_id,
        job_id=admitted.job_id,
        context=_context(device_id, "ambiguous-claim"),
    )

    outcome = record_tr069_command_observation(
        db_session,
        Tr069DeliveryObservation(
            context=_context(device_id, "ambiguous-outcome"),
            job_id=admitted.job_id,
            operation_id=admitted.operation_id,
            state=Tr069DeliveryState.unverified,
            reason="ACS response ended without confirmable delivery.",
        ),
    )
    second_claim = claim_tr069_command_execution(
        db_session,
        operation_id=admitted.operation_id,
        job_id=admitted.job_id,
        context=_context(device_id, "ambiguous-replay"),
    )
    with pytest.raises(Tr069CommandError) as review_required:
        request_tr069_command(
            db_session,
            _reboot_request(device_id, "retry-before-fresh-inform"),
        )

    operation = db_session.get(NetworkOperation, admitted.operation_id)
    assert outcome.status is Tr069JobStatus.unverified
    assert operation is not None
    assert operation.status is NetworkOperationStatus.warning
    assert second_claim.executable is False
    assert second_claim.reason == "operation_terminal"
    assert review_required.value.code == (
        "network.tr069_commands.device_state_review_required"
    )

    device = db_session.get(Tr069CpeDevice, device_id)
    assert device is not None
    assert operation.completed_at is not None
    device.last_inform_at = operation.completed_at + timedelta(seconds=1)
    db_session.commit()
    reviewed_retry = request_tr069_command(
        db_session,
        _reboot_request(device_id, "retry-after-fresh-inform"),
    )
    assert reviewed_retry.operation_id != admitted.operation_id


def test_reconciler_repairs_active_projection_after_terminal_dispatch_failure(
    db_session,
):
    device_id = _device(db_session)
    admitted = request_tr069_command(
        db_session,
        _reboot_request(device_id, "interrupted-admit"),
    )
    claim_tr069_command_execution(
        db_session,
        operation_id=admitted.operation_id,
        job_id=admitted.job_id,
        context=_context(device_id, "interrupted-claim"),
    )
    network_operations.mark_failed(
        db_session,
        str(admitted.operation_id),
        "Worker completion was interrupted.",
    )
    db_session.commit()

    outcome = record_tr069_command_observation(
        db_session,
        Tr069DeliveryObservation(
            context=_context(device_id, "interrupted-repair"),
            job_id=admitted.job_id,
            operation_id=admitted.operation_id,
            state=Tr069DeliveryState.unverified,
            reason="Worker stopped after ACS delivery may have occurred.",
        ),
    )

    assert outcome.status is Tr069JobStatus.unverified
    job = db_session.get(Tr069Job, admitted.job_id)
    assert job is not None
    assert job.secure_payload is None


@pytest.mark.parametrize(
    "url",
    (
        "ftp://firmware.test.invalid/image.bin",
        "https://user:password@firmware.test.invalid/image.bin",
    ),
)
def test_firmware_admission_rejects_unsafe_urls(db_session, url):
    device_id = _device(db_session)

    with pytest.raises(Tr069CommandError) as exc_info:
        request_tr069_command(
            db_session,
            Tr069CommandRequest(
                context=_context(device_id, "unsafe-firmware"),
                device_id=device_id,
                name="Firmware Update",
                kind=Tr069CommandKind.download,
                download=Tr069Download(url=url),
            ),
        )

    assert exc_info.value.code == "network.tr069_commands.invalid_download"


def test_acs_pending_work_preflight_never_reuses_or_replaces_a_task(monkeypatch):
    create_calls: list[object] = []
    client = SimpleNamespace(
        get_pending_tasks=lambda _device_id: [{"_id": "someone-elses-task"}],
        create_task=lambda *args, **kwargs: create_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "app.services.genieacs_client.create_genieacs_client",
        lambda _url: client,
    )
    plan = SimpleNamespace(
        server_url="https://acs.test.invalid",
        genieacs_device_id="device-1",
        kind=Tr069CommandKind.reboot,
        object_names=(),
        parameter_values=(),
        download=None,
    )

    with pytest.raises(_Tr069SubmissionBlocked):
        _submit_tr069_plan(plan)

    assert create_calls == []
