"""Hosted unit canaries for the count-only observation/policy boundary."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.celery_queue_contract import DEPLOYMENT_QUEUE_NAMES
from app.services.deployment_writer_drain import (
    DECLARED_DEPLOYMENT_QUEUES,
    DrainBudgets,
    DrainMode,
    DrainObservation,
    DrainObservationError,
    DrainOutcome,
    DrainRefusal,
    DrainRequest,
    QueuePopulation,
    UnacknowledgedPopulation,
    WorkerNodeId,
    WorkerPopulation,
    observe_writer_drain,
    wait_for_stable_writer_drain,
)
from app.services.deployment_writer_drain_transport import (
    RedisDrainConfiguration,
    _known_empty_error,
    _population_counts,
    configured_redis_drain,
)
from scripts.observe_writer_drain import main, result_json

NODE = WorkerNodeId("celery@incumbent")


def request() -> DrainRequest:
    return DrainRequest(DrainMode.WORKERS_AND_BROKER, (NODE,), DrainBudgets(1, 30, 5))


def observation() -> DrainObservation:
    now = datetime.now(UTC)
    return DrainObservation(
        DrainMode.WORKERS_AND_BROKER,
        now,
        now,
        (WorkerPopulation(NODE, 0, 0, 0),),
        tuple(QueuePopulation(q, 0) for q in DECLARED_DEPLOYMENT_QUEUES),
        UnacknowledgedPopulation(0, 0),
    )


class Clock:
    now: float = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Source:
    def __init__(self, *observations: DrainObservation | DrainObservationError) -> None:
        self.observations = iter(observations)
        self.calls = 0
        self.timeouts: list[float] = []

    def observe(self, *, request: DrainRequest, timeout: float) -> DrainObservation:
        self.calls += 1
        self.timeouts.append(timeout)
        next_observation = next(self.observations)
        if isinstance(next_observation, DrainObservationError):
            raise next_observation
        return next_observation


@pytest.mark.parametrize("field", ("active", "reserved", "scheduled"))
def test_every_worker_population_blocks_empty(field: str) -> None:
    busy_worker = replace(observation().workers[0], **{field: 1})
    result = observe_writer_drain(
        request=request(), source=Source(replace(observation(), workers=(busy_worker,)))
    )
    assert result.outcome is DrainOutcome.OBSERVED_BUSY


@pytest.mark.parametrize("queue", DEPLOYMENT_QUEUE_NAMES)
def test_every_declared_queue_blocks_empty(queue: str) -> None:
    busy = replace(
        observation(),
        queues=tuple(
            QueuePopulation(q, int(q.value == queue))
            for q in DECLARED_DEPLOYMENT_QUEUES
        ),
    )
    assert (
        observe_writer_drain(request=request(), source=Source(busy)).outcome
        is DrainOutcome.OBSERVED_BUSY
    )


@pytest.mark.parametrize(
    "unacked", (UnacknowledgedPopulation(1, 0), UnacknowledgedPopulation(0, 1))
)
def test_orphan_hash_and_index_independently_block(
    unacked: UnacknowledgedPopulation,
) -> None:
    assert (
        observe_writer_drain(
            request=request(),
            source=Source(replace(observation(), unacknowledged=unacked)),
        ).outcome
        is DrainOutcome.OBSERVED_BUSY
    )


def test_late_enqueue_resets_two_zero_stability() -> None:
    zero = observation()
    busy = replace(zero, unacknowledged=UnacknowledgedPopulation(1, 1))
    clock = Clock()
    source = Source(zero, busy, zero, zero)
    result = wait_for_stable_writer_drain(request=request(), source=source, clock=clock)
    assert result.outcome is DrainOutcome.STABLE_EMPTY
    assert source.calls == 4
    assert clock.now == 15
    assert source.timeouts == [30, 25, 20, 15]


def test_unknown_observation_refuses_without_retry() -> None:
    source = Source(
        observation(), DrainObservationError(DrainRefusal.MISSING_NODE), observation()
    )
    result = wait_for_stable_writer_drain(
        request=request(), source=source, clock=Clock()
    )
    assert result.refusal is DrainRefusal.MISSING_NODE
    assert source.calls == 2
    assert result.observation is None


def test_deadline_cannot_accept_late_second_zero() -> None:
    clock = Clock()

    class SlowSource(Source):
        def observe(self, *, request: DrainRequest, timeout: float) -> DrainObservation:
            clock.now += 16
            return observation()

    result = wait_for_stable_writer_drain(
        request=request(), source=SlowSource(), clock=clock
    )
    assert result.outcome is DrainOutcome.DEADLINE_EXCEEDED


@pytest.mark.parametrize(
    "raw,code",
    (
        ([], DrainRefusal.MISSING_NODE),
        (
            [{"celery@incumbent": []}, {"celery@incumbent": []}],
            DrainRefusal.DUPLICATE_NODE,
        ),
        ([{"celery@forged": []}], DrainRefusal.UNEXPECTED_NODE),
        ({"celery@incumbent": []}, DrainRefusal.MALFORMED_REPLY),
        ([{"celery@incumbent": {"error": "secret"}}], DrainRefusal.MALFORMED_REPLY),
        ([{"celery@incumbent": [{}]}], DrainRefusal.MALFORMED_REPLY),
        ([{"celery@incumbent": [], "celery@extra": []}], DrainRefusal.MALFORMED_REPLY),
    ),
)
def test_raw_replies_preserve_exact_coverage(raw: object, code: DrainRefusal) -> None:
    with pytest.raises(DrainObservationError) as error:
        _population_counts(raw=raw, expected=(NODE,))
    assert error.value.code is code


def test_scheduled_counts_discard_payloads() -> None:
    raw = [
        {
            NODE.value: [
                {
                    "eta": "future",
                    "request": {"id": "task-id", "args": "private-customer-data"},
                }
            ]
        }
    ]
    assert _population_counts(raw=raw, expected=(NODE,), scheduled=True) == ((NODE, 1),)


@pytest.mark.parametrize(
    "config",
    (
        {"broker_url": "redis://localhost/0", "global_keyprefix": "prefix:"},
        {"broker_url": "sentinel://localhost/0"},
        {"broker_url": "rediss://localhost/0"},
        {"broker_url": "redis://localhost/0?global_keyprefix=x"},
        {"broker_url": "redis://localhost"},
    ),
)
def test_unproved_transport_refuses(config: dict[str, str]) -> None:
    with pytest.raises(DrainObservationError) as error:
        RedisDrainConfiguration(**config)
    assert error.value.code is DrainRefusal.UNSUPPORTED_CONFIG


def test_broker_resolution_matches_explicit_scheduler_precedence() -> None:
    config = configured_redis_drain(
        environment={
            "CELERY_BROKER_URL": "redis://user:secret@broker/2",
            "REDIS_URL": "redis://cache/0",
        }
    )
    assert config.broker_url.endswith("broker/2")
    assert "secret" not in repr(config)
    with pytest.raises(DrainObservationError):
        configured_redis_drain(environment={})


def test_only_exact_known_empty_passive_404_is_empty() -> None:
    from amqp.exceptions import ChannelError

    known = ChannelError(
        "NOT_FOUND - no queue 'billing' in vhost '0'",
        (50, 10),
        "Channel.queue_declare",
        "404",
    )
    assert _known_empty_error(error=known, queue="billing", virtual_host="0")
    assert not _known_empty_error(error=known, queue="nin", virtual_host="0")
    assert not _known_empty_error(
        error=ChannelError("arbitrary", (50, 10), "Channel.queue_declare", "404"),
        queue="billing",
        virtual_host="0",
    )
    assert not _known_empty_error(
        error=ConnectionError("secret"), queue="billing", virtual_host="0"
    )


def test_mode_scope_and_complete_queue_contract_cannot_be_substituted() -> None:
    with pytest.raises(DrainObservationError):
        DrainRequest(DrainMode.BROKER_ONLY, (NODE,))
    with pytest.raises(DrainObservationError):
        DrainRequest(DrainMode.WORKERS_AND_BROKER, ())
    with pytest.raises(DrainObservationError):
        DrainRequest(DrainMode.WORKERS_AND_BROKER, (NODE, NODE))
    with pytest.raises(DrainObservationError):
        replace(request(), declared_queues=("billing",))
    mismatched = replace(observation(), mode=DrainMode.BROKER_ONLY, workers=())
    assert (
        observe_writer_drain(request=request(), source=Source(mismatched)).refusal
        is DrainRefusal.INVALID_OBSERVATION
    )


@pytest.mark.parametrize("value", (float("nan"), float("inf"), 0, -1, 6))
def test_invalid_inspection_budgets_refuse(value: float) -> None:
    with pytest.raises(DrainObservationError):
        DrainBudgets(inspection_timeout=value)


def test_cli_invalid_input_is_safe_bounded_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "inspect",
                "--mode",
                "workers-and-broker",
                "--expected-node",
                "celery@ok",
                "--inspection-timeout",
                "secret",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "secret" not in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["refusal"] == "invalid-input"


def test_evidence_contains_only_count_contract() -> None:
    result = observe_writer_drain(request=request(), source=Source(observation()))
    encoded = json.dumps(result_json(result))
    assert "broker_url" not in encoded
    assert "args" not in encoded
    assert "kwargs" not in encoded
    assert result_json(result)["mode"] == "workers-and-broker"


def run_isolated_signal_canary(code: str) -> None:
    # pytest-timeout owns the parent's ITIMER_REAL. A fresh interpreter lets
    # the actual POSIX guard run without disabling or replacing that timer.
    prelude = """
import json
import signal
import time
from unittest.mock import patch
from app.services.deployment_writer_drain_transport import RedisCeleryDrainSource
from tests.test_deployment_writer_drain import (
    DrainObservation, DrainObservationError, DrainRefusal, DrainRequest,
    RedisDrainConfiguration, observation, observe_writer_drain, request, result_json,
)
source = RedisCeleryDrainSource(
    configuration=RedisDrainConfiguration(broker_url="redis://127.0.0.1:9/10")
)
"""
    process = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(prelude) + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert process.returncode == 0, process.stderr


def test_hard_observation_deadline_interrupts_and_restores_alarm() -> None:
    run_isolated_signal_canary("""
    def blocked(*, request: DrainRequest) -> DrainObservation:
        time.sleep(2)
        return observation()
    previous_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with patch.object(source, "_observe", blocked):
        try:
            source.observe(request=request(), timeout=0.03)
        except DrainObservationError as error:
            assert error.code is DrainRefusal.INSPECTION_TIMEOUT
        else:
            raise AssertionError("blocking observation did not time out")
    assert time.monotonic() - started < 1
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    """)


def test_transport_exception_text_never_crosses_boundary() -> None:
    run_isolated_signal_canary("""
    def failed(*, request: DrainRequest) -> DrainObservation:
        raise RuntimeError("private-transport-payload")
    with patch.object(source, "_observe", failed):
        result = observe_writer_drain(request=request(), source=source)
    assert result.refusal is DrainRefusal.TRANSPORT_UNAVAILABLE
    assert "private-transport-payload" not in json.dumps(result_json(result))
    """)


def test_preexisting_alarm_refuses_without_replacing_it() -> None:
    run_isolated_signal_canary("""
    def parent_alarm(signum: int, frame: object) -> None:
        raise AssertionError("existing alarm expired")
    signal.signal(signal.SIGALRM, parent_alarm)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        with patch.object(source, "_observe") as observed:
            result = observe_writer_drain(request=request(), source=source)
        assert result.refusal is DrainRefusal.UNSUPPORTED_RUNTIME
        observed.assert_not_called()
        assert signal.getsignal(signal.SIGALRM) is parent_alarm
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    """)


@pytest.mark.parametrize(
    "overlay", ["CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL"]
)
def test_split_broker_overlays_refuse_empty_base_namespace(overlay: str) -> None:
    with pytest.raises(DrainObservationError) as caught:
        configured_redis_drain(
            environment={
                "CELERY_BROKER_URL": "redis://localhost:6379/10",
                overlay: "redis://localhost:6379/11",
            }
        )
    assert caught.value.code is DrainRefusal.UNSUPPORTED_CONFIG
