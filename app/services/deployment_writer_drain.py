"""Count-only incumbent drain observations; no activation or financial authority."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.celery_queue_contract import DEPLOYMENT_QUEUE_NAMES


class DrainMode(StrEnum):
    WORKERS_AND_BROKER = "workers-and-broker"
    BROKER_ONLY = "broker-only"


class DrainOutcome(StrEnum):
    OBSERVED_EMPTY = "observed-empty"
    OBSERVED_BUSY = "observed-busy"
    STABLE_EMPTY = "stable-empty"
    REFUSED = "refused"
    DEADLINE_EXCEEDED = "deadline-exceeded"


class DrainRefusal(StrEnum):
    INVALID_INPUT = "invalid-input"
    UNSUPPORTED_CONFIG = "unsupported-config"
    UNSUPPORTED_RUNTIME = "unsupported-runtime"
    DEPENDENCY_MISMATCH = "dependency-mismatch"
    TRANSPORT_UNAVAILABLE = "transport-unavailable"
    INSPECTION_TIMEOUT = "inspection-timeout"
    MALFORMED_REPLY = "malformed-reply"
    MISSING_NODE = "missing-node"
    DUPLICATE_NODE = "duplicate-node"
    UNEXPECTED_NODE = "unexpected-node"
    INVALID_OBSERVATION = "invalid-observation"


class DrainObservationError(Exception):
    """Only a closed, non-sensitive code crosses the transport boundary."""

    def __init__(self, code: DrainRefusal) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, order=True)
class WorkerNodeId:
    value: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,255}", self.value):
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)


@dataclass(frozen=True)
class DeploymentQueueName:
    value: str

    def __post_init__(self) -> None:
        if self.value not in DEPLOYMENT_QUEUE_NAMES:
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)


DECLARED_DEPLOYMENT_QUEUES = tuple(
    DeploymentQueueName(q) for q in DEPLOYMENT_QUEUE_NAMES
)


@dataclass(frozen=True)
class DrainBudgets:
    inspection_timeout: float = 5.0
    drain_budget: float = 600.0
    stability_interval: float = 5.0

    def __post_init__(self) -> None:
        values = (self.inspection_timeout, self.drain_budget, self.stability_interval)
        if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        if not 0 < self.inspection_timeout <= 5:
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        if not 5 <= self.stability_interval <= 60:
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        if not self.stability_interval < self.drain_budget <= 3600:
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)


@dataclass(frozen=True)
class DrainRequest:
    mode: DrainMode
    expected_nodes: tuple[WorkerNodeId, ...]
    budgets: DrainBudgets = DrainBudgets()
    declared_queues: tuple[DeploymentQueueName, ...] = DECLARED_DEPLOYMENT_QUEUES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.mode, DrainMode)
            or not isinstance(self.budgets, DrainBudgets)
            or type(self.expected_nodes) is not tuple
            or any(not isinstance(node, WorkerNodeId) for node in self.expected_nodes)
        ):
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        if self.declared_queues != DECLARED_DEPLOYMENT_QUEUES:
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        if len(self.expected_nodes) != len(set(self.expected_nodes)):
            raise DrainObservationError(DrainRefusal.DUPLICATE_NODE)
        if self.mode is DrainMode.WORKERS_AND_BROKER:
            if not 1 <= len(self.expected_nodes) <= 64:
                raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        elif self.expected_nodes:
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)


@dataclass(frozen=True)
class WorkerPopulation:
    node: WorkerNodeId
    active: int
    reserved: int
    scheduled: int


@dataclass(frozen=True)
class QueuePopulation:
    queue: DeploymentQueueName
    ready: int


@dataclass(frozen=True)
class UnacknowledgedPopulation:
    hash_entries: int
    index_entries: int


@dataclass(frozen=True)
class DrainObservation:
    mode: DrainMode
    started_at: datetime
    completed_at: datetime
    workers: tuple[WorkerPopulation, ...]
    queues: tuple[QueuePopulation, ...]
    unacknowledged: UnacknowledgedPopulation

    @property
    def empty(self) -> bool:
        return (
            all(not (w.active or w.reserved or w.scheduled) for w in self.workers)
            and all(q.ready == 0 for q in self.queues)
            and self.unacknowledged.hash_entries == 0
            and self.unacknowledged.index_entries == 0
        )


@dataclass(frozen=True)
class DrainResult:
    mode: DrainMode
    outcome: DrainOutcome
    refusal: DrainRefusal | None = None
    observation: DrainObservation | None = None
    previous_zero: DrainObservation | None = None


class DrainSource(Protocol):
    def observe(self, *, request: DrainRequest, timeout: float) -> DrainObservation: ...


class DrainClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemDrainClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def valid_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _validate_observation(
    *, request: DrainRequest, observation: DrainObservation
) -> None:
    workers = observation.workers
    queues = observation.queues
    counts = [c for w in workers for c in (w.active, w.reserved, w.scheduled)]
    counts.extend(q.ready for q in queues)
    counts.extend(
        (
            observation.unacknowledged.hash_entries,
            observation.unacknowledged.index_entries,
        )
    )
    if (
        observation.mode is not request.mode
        or tuple(sorted(w.node for w in workers))
        != tuple(sorted(request.expected_nodes))
        or tuple(q.queue for q in queues) != request.declared_queues
        or any(not valid_count(c) for c in counts)
        or observation.started_at.utcoffset() is None
        or observation.completed_at.utcoffset() is None
        or observation.completed_at < observation.started_at
    ):
        raise DrainObservationError(DrainRefusal.INVALID_OBSERVATION)


def observe_writer_drain(
    *, request: DrainRequest, source: DrainSource, timeout: float | None = None
) -> DrainResult:
    """One complete observation; a refused observation is never retried as empty."""
    try:
        effective_timeout = request.budgets.drain_budget if timeout is None else timeout
        if (
            not math.isfinite(effective_timeout)
            or not 0 < effective_timeout <= request.budgets.drain_budget
        ):
            raise DrainObservationError(DrainRefusal.INVALID_INPUT)
        observation = source.observe(request=request, timeout=effective_timeout)
        _validate_observation(request=request, observation=observation)
    except DrainObservationError as exc:
        return DrainResult(request.mode, DrainOutcome.REFUSED, refusal=exc.code)
    return DrainResult(
        request.mode,
        DrainOutcome.OBSERVED_EMPTY
        if observation.empty
        else DrainOutcome.OBSERVED_BUSY,
        observation=observation,
    )


def wait_for_stable_writer_drain(
    *, request: DrainRequest, source: DrainSource, clock: DrainClock | None = None
) -> DrainResult:
    """Two complete zeros separated after the first finishes, within one deadline."""
    clock = clock or SystemDrainClock()
    deadline = clock.monotonic() + request.budgets.drain_budget
    previous_zero: DrainObservation | None = None
    last: DrainObservation | None = None
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return DrainResult(
                request.mode, DrainOutcome.DEADLINE_EXCEEDED, observation=last
            )
        result = observe_writer_drain(request=request, source=source, timeout=remaining)
        if result.outcome is DrainOutcome.REFUSED:
            return result
        last = result.observation
        if clock.monotonic() >= deadline:
            return DrainResult(
                request.mode, DrainOutcome.DEADLINE_EXCEEDED, observation=last
            )
        if last is not None and last.empty:
            if previous_zero is not None:
                return DrainResult(
                    request.mode,
                    DrainOutcome.STABLE_EMPTY,
                    observation=last,
                    previous_zero=previous_zero,
                )
            previous_zero = last
        else:
            previous_zero = None
        clock.sleep(
            min(
                request.budgets.stability_interval,
                max(0.0, deadline - clock.monotonic()),
            )
        )
