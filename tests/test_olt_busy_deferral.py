"""G4: a busy OLT defers the apply (capped) and never strands a dispatch.

``reconcile_ont`` returns ``olt_busy`` before reading or writing anything when
every SSH slot is taken. The configuration lifecycle must turn that into a
delayed re-dispatch of the same apply, count the deferrals in the operation
payload, and fail the revision with ``failure_code="olt_busy"`` (operator
Retry) at the cap. It must do this by returning an outcome, never by raising:
a worker exception after acknowledgement is exactly what produces the
``reconciliation_needed`` dispatches this change exists to stop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.ont_service_configuration import OntServiceConfigurationPhase
from app.services.network import ont_service_configuration as svc
from app.services.network.olt_ssh_limiter import LimiterConfig, set_config_override
from app.services.network_operation_dispatch import NetworkOperationCommand


@pytest.fixture
def harness(monkeypatch):
    calls = SimpleNamespace(waiting=[], failed=[], staged=[], events=[], emitted=[])
    monkeypatch.setattr(
        svc.network_operations,
        "mark_waiting",
        lambda db, op_id, reason: calls.waiting.append(reason),
    )
    monkeypatch.setattr(
        svc.network_operations,
        "mark_failed",
        lambda db, op_id, message, output_payload=None: calls.failed.append(
            (message, output_payload)
        ),
    )
    monkeypatch.setattr(
        svc,
        "stage_dispatch",
        lambda db, op, command, **kw: calls.staged.append((command, kw)),
    )
    monkeypatch.setattr(
        svc, "_record_execution_event", lambda db, **kw: calls.events.append(kw)
    )
    monkeypatch.setattr(svc, "emit_event", lambda *a, **k: calls.emitted.append(a))
    set_config_override(LimiterConfig(busy_max_deferrals=5))
    return calls


def _parts():
    return dict(
        command=SimpleNamespace(context=SimpleNamespace(actor="system")),
        ont=SimpleNamespace(id=uuid.uuid4()),
        assignment=SimpleNamespace(id=uuid.uuid4(), subscriber_id=uuid.uuid4()),
        head=SimpleNamespace(
            id=uuid.uuid4(),
            phase=OntServiceConfigurationPhase.applying,
            waiting_reason=None,
            failure_code=None,
            failure_message=None,
        ),
        revision=SimpleNamespace(
            revision=3, phase=OntServiceConfigurationPhase.applying
        ),
        operation=SimpleNamespace(id=uuid.uuid4(), input_payload={}),
        failure=SimpleNamespace(
            reason="olt_busy", message="olt_busy: all 2 SSH session slot(s) in use"
        ),
    )


def test_each_busy_pass_is_deferred_with_a_delayed_apply(harness):
    parts = _parts()
    db = SimpleNamespace(flush=lambda: None)
    before = datetime.now(UTC)

    for attempt in range(1, 6):
        outcome = svc._defer_for_olt_busy(db, **parts)
        assert outcome.executed is True and outcome.stale is False
        assert outcome.phase is OntServiceConfigurationPhase.queued
        assert parts["operation"].input_payload["olt_busy_deferrals"] == attempt
        command, kwargs = harness.staged[-1]
        assert command is NetworkOperationCommand.ont_service_config_apply_v1
        assert kwargs["dispatch_key"] == f"olt_busy:{attempt}"
        assert kwargs["not_before"] >= before + timedelta(seconds=30 * attempt)
        assert parts["head"].waiting_reason == "awaiting_olt_session"
        assert parts["head"].failure_code is None

    assert harness.waiting == ["awaiting_olt_session"] * 5
    assert harness.failed == []


def test_deferral_cap_fails_with_olt_busy_and_stages_nothing_more(harness):
    parts = _parts()
    parts["operation"].input_payload = {"olt_busy_deferrals": 5}
    db = SimpleNamespace(flush=lambda: None)

    outcome = svc._defer_for_olt_busy(db, **parts)

    assert outcome.phase is OntServiceConfigurationPhase.failed
    assert parts["head"].failure_code == "olt_busy"
    assert parts["head"].waiting_reason is None
    assert parts["revision"].phase is OntServiceConfigurationPhase.failed
    assert harness.staged == []
    assert len(harness.failed) == 1
    message, payload = harness.failed[0]
    assert "Retry" in message
    assert payload["failure_code"] == "olt_busy"
    assert payload["olt_busy_deferrals"] == 5


def test_cap_comes_from_the_setting(harness):
    set_config_override(LimiterConfig(busy_max_deferrals=2))
    parts = _parts()
    db = SimpleNamespace(flush=lambda: None)

    assert (
        svc._defer_for_olt_busy(db, **parts).phase
        is OntServiceConfigurationPhase.queued
    )
    assert (
        svc._defer_for_olt_busy(db, **parts).phase
        is OntServiceConfigurationPhase.queued
    )
    assert (
        svc._defer_for_olt_busy(db, **parts).phase
        is OntServiceConfigurationPhase.failed
    )


def test_resumed_apply_uses_its_own_dispatch_keys(harness):
    parts = _parts()
    parts["operation"].input_payload = {"resume_count": 2}
    svc._defer_for_olt_busy(SimpleNamespace(flush=lambda: None), **parts)

    assert harness.staged[-1][1]["dispatch_key"] == "olt_busy:1@r2"
