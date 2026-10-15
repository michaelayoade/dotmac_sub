"""Pinned Redis/Celery adapter. Task payloads never leave reply validation."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit

from app.services.deployment_writer_drain import (
    DrainMode,
    DrainObservation,
    DrainObservationError,
    DrainRefusal,
    DrainRequest,
    QueuePopulation,
    UnacknowledgedPopulation,
    WorkerNodeId,
    WorkerPopulation,
    valid_count,
)


@dataclass(frozen=True)
class RedisDrainConfiguration:
    broker_url: str = field(repr=False)
    global_keyprefix: str = ""
    unacked_key: str = "unacked"
    unacked_index_key: str = "unacked_index"
    priority_steps: tuple[int, ...] = (0, 3, 6, 9)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.broker_url)
            supported = (
                parsed.scheme == "redis"
                and bool(parsed.hostname)
                and parsed.port != 0
                and not parsed.query
                and not parsed.fragment
                and parsed.path.startswith("/")
                and parsed.path[1:].isdigit()
                and not self.global_keyprefix
                and bool(self.unacked_key)
                and bool(self.unacked_index_key)
                and self.unacked_key != self.unacked_index_key
                and self.priority_steps == tuple(sorted(set(self.priority_steps)))
                and bool(self.priority_steps)
                and self.priority_steps[0] == 0
                and all(type(p) is int and 0 <= p <= 9 for p in self.priority_steps)
            )
        except (ValueError, TypeError):
            supported = False
        if not supported:
            raise DrainObservationError(DrainRefusal.UNSUPPORTED_CONFIG)


def configured_redis_drain(
    *, environment: Mapping[str, str]
) -> RedisDrainConfiguration:
    """Scheduler's env-owned precedence, refusing its unsafe absent-URL default.

    No scheduler/settings import: get_celery_config opens a database session.
    Unsupported overlays refuse rather than silently changing the observed DB.
    """
    if any(
        environment.get(key)
        for key in (
            "CELERY_BROKER_TRANSPORT_OPTIONS",
            "CELERY_CONFIG_MODULE",
            "CELERY_BROKER_READ_URL",
            "CELERY_BROKER_WRITE_URL",
        )
    ):
        raise DrainObservationError(DrainRefusal.UNSUPPORTED_CONFIG)
    broker = environment.get("CELERY_BROKER_URL") or environment.get("REDIS_URL")
    if not broker:
        raise DrainObservationError(DrainRefusal.UNSUPPORTED_CONFIG)
    return RedisDrainConfiguration(broker_url=broker)


class _ObservationTimedOut(BaseException):
    pass


def _on_alarm(_signum: int, _frame: object) -> None:
    raise _ObservationTimedOut


@contextmanager
def _hard_timeout(seconds: float) -> Iterator[None]:
    """POSIX main-thread guard bounds retries, reply floods, DNS and cleanup too."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    effective = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    signal.signal(signal.SIGALRM, _on_alarm)
    # BaseException bypasses library retry catches. Repeating expiry interrupts
    # blocking context-manager cleanup after the first expiry as well.
    signal.setitimer(signal.ITIMER_REAL, max(0.000001, effective), 0.05)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )


def _population_counts(
    *, raw: object, expected: tuple[WorkerNodeId, ...], scheduled: bool = False
) -> tuple[tuple[WorkerNodeId, int], ...]:
    """Raw replies retain duplicates; do not flatten through Inspect._prepare."""
    if not isinstance(raw, list):
        raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
    expected_by_name = {node.value: node for node in expected}
    counts: dict[WorkerNodeId, int] = {}
    for reply in raw:
        if not isinstance(reply, dict) or len(reply) != 1:
            raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
        name, population = next(iter(reply.items()))
        if not isinstance(name, str) or name not in expected_by_name:
            raise DrainObservationError(DrainRefusal.UNEXPECTED_NODE)
        node = expected_by_name[name]
        if node in counts:
            raise DrainObservationError(DrainRefusal.DUPLICATE_NODE)
        if not isinstance(population, list):
            raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
        for entry in population:
            if not isinstance(entry, dict):
                raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
            request = entry.get("request") if scheduled else entry
            if (
                not isinstance(request, dict)
                or not isinstance(request.get("id"), str)
                or not request["id"]
            ):
                raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
        counts[node] = len(population)
    if set(counts) != set(expected):
        raise DrainObservationError(DrainRefusal.MISSING_NODE)
    return tuple((node, counts[node]) for node in expected)


def _known_empty_error(*, error: Exception, queue: str, virtual_host: str) -> bool:
    """Exact Kombu virtual passive-declare absence; never arbitrary 404/errors."""
    from amqp.exceptions import ChannelError

    return (
        type(error) is ChannelError
        and error.reply_code == "404"
        and error.method_sig == (50, 10)
        and error.method_name == "Channel.queue_declare"
        and error.reply_text
        == f"NOT_FOUND - no queue {queue!r} in vhost {virtual_host!r}"
    )


class RedisCeleryDrainSource:
    def __init__(self, *, configuration: RedisDrainConfiguration) -> None:
        self._configuration = configuration

    def observe(self, *, request: DrainRequest, timeout: float) -> DrainObservation:
        if threading.current_thread() is not threading.main_thread() or not hasattr(
            signal, "setitimer"
        ):
            raise DrainObservationError(DrainRefusal.UNSUPPORTED_RUNTIME)
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise DrainObservationError(DrainRefusal.UNSUPPORTED_RUNTIME)
        try:
            with _hard_timeout(timeout):
                return self._observe(request=request)
        except DrainObservationError:
            raise
        except _ObservationTimedOut:
            raise DrainObservationError(DrainRefusal.INSPECTION_TIMEOUT) from None
        except Exception:
            # Library exception text can contain endpoints, credentials or payloads.
            raise DrainObservationError(DrainRefusal.TRANSPORT_UNAVAILABLE) from None

    def _observe(self, *, request: DrainRequest) -> DrainObservation:
        for package, pinned in (
            ("celery", "5.4.0"),
            ("kombu", "5.6.2"),
            ("redis", "5.0.4"),
        ):
            try:
                installed = version(package)
            except PackageNotFoundError:
                raise DrainObservationError(DrainRefusal.DEPENDENCY_MISMATCH) from None
            if installed != pinned:
                raise DrainObservationError(DrainRefusal.DEPENDENCY_MISMATCH)
        from celery import Celery
        from kombu import Connection

        started = datetime.now(UTC)
        config = self._configuration
        options = {
            "global_keyprefix": config.global_keyprefix,
            "unacked_key": config.unacked_key,
            "unacked_index_key": config.unacked_index_key,
            "priority_steps": list(config.priority_steps),
            "socket_timeout": request.budgets.inspection_timeout,
            "socket_connect_timeout": request.budgets.inspection_timeout,
            "retry_on_timeout": False,
        }
        workers: tuple[WorkerPopulation, ...] = ()
        with Connection(
            config.broker_url,
            transport_options=options,
            connect_timeout=request.budgets.inspection_timeout,
        ) as connection:
            if (
                connection.transport.driver_type != "redis"
                or connection.transport.driver_name != "redis"
            ):
                raise DrainObservationError(DrainRefusal.UNSUPPORTED_CONFIG)
            if request.mode is DrainMode.WORKERS_AND_BROKER:
                # Redis pidbox polling normally restores globally expired
                # financial deliveries. Disable ack emulation ONLY on this
                # separate inspect transport; incumbent workers and the broker
                # count channel retain their actual acknowledgement settings.
                control_options = {
                    **options,
                    "ack_emulation": False,
                    # Kombu 5.6.2 retries an empty Redis poll with the original
                    # drain_events timeout, not its remaining time. Its default
                    # one-second BRPOP can therefore overrun a two-second
                    # inspection. This public option shortens both BRPOP and
                    # the sleep between empty polls, including pending BRPOP
                    # cleanup. Keep it separate from financial transports.
                    "polling_interval": min(
                        0.05, request.budgets.inspection_timeout * 0.05
                    ),
                }
                with (
                    Celery(
                        "deployment_drain_observer",
                        broker=config.broker_url,
                        set_as_current=False,
                    ) as control_app,
                    Connection(
                        config.broker_url,
                        transport_options=control_options,
                        connect_timeout=request.budgets.inspection_timeout,
                    ) as control_connection,
                ):
                    control_app.conf.update(
                        broker_transport_options=control_options,
                        broker_connection_max_retries=0,
                        accept_content=["json"],
                        task_serializer="json",
                    )
                    populations: list[tuple[tuple[WorkerNodeId, int], ...]] = []
                    for command in ("active", "reserved", "scheduled"):
                        with _hard_timeout(request.budgets.inspection_timeout):
                            raw = control_app.control.broadcast(
                                command,
                                arguments={"safe": True} if command == "active" else {},
                                # Query all broker-visible workers so an extra
                                # respondent cannot hide behind destination
                                # filtering, then require the exact census.
                                destination=None,
                                connection=control_connection,
                                reply=True,
                                # Pidbox's timeout is inactivity per reply. Leave
                                # headroom below the hard whole-request deadline.
                                timeout=request.budgets.inspection_timeout * 0.75,
                                limit=0,
                            )
                        populations.append(
                            _population_counts(
                                raw=raw,
                                expected=request.expected_nodes,
                                scheduled=command == "scheduled",
                            )
                        )
                        del raw
                    workers = tuple(
                        WorkerPopulation(
                            node,
                            populations[0][i][1],
                            populations[1][i][1],
                            populations[2][i][1],
                        )
                        for i, node in enumerate(request.expected_nodes)
                    )
            with connection.channel() as channel:
                if channel.global_keyprefix or not channel.ack_emulation:
                    raise DrainObservationError(DrainRefusal.UNSUPPORTED_CONFIG)
                queues: list[QueuePopulation] = []
                for queue in request.declared_queues:
                    try:
                        declared = channel.queue_declare(
                            queue=queue.value, passive=True
                        )
                        count = declared.message_count
                    except Exception as exc:
                        if not _known_empty_error(
                            error=exc,
                            queue=queue.value,
                            virtual_host=connection.virtual_host or "/",
                        ):
                            raise
                        count = 0
                    if not valid_count(count):
                        raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
                    queues.append(QueuePopulation(queue, count))
                # Public Redis operations using the actual configured transport keys.
                hash_entries = channel.client.hlen(channel.unacked_key)
                index_entries = channel.client.zcard(channel.unacked_index_key)
                if not valid_count(hash_entries) or not valid_count(index_entries):
                    raise DrainObservationError(DrainRefusal.MALFORMED_REPLY)
                unacked = UnacknowledgedPopulation(hash_entries, index_entries)
        return DrainObservation(
            request.mode, started, datetime.now(UTC), workers, tuple(queues), unacked
        )
