"""Dedicated required hosted lane: real Redis/Celery drain canaries.

Requires an exclusive disposable Redis DB 10..15 on a hosted test service.
The test fixture may mutate that test DB; the observer must remain read-only
except for Celery's necessary inspect request/reply control traffic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from celery import Celery
from celery.contrib.testing.worker import TestWorkController as CeleryTestWorkController
from celery.contrib.testing.worker import start_worker
from kombu import Connection, Producer, Queue
from kombu.transport.redis import Transport as RedisTransport
from redis import Redis
from redis.client import Pipeline

from app.celery_queue_contract import DEPLOYMENT_QUEUE_NAMES
from app.services.deployment_writer_drain import (
    DrainBudgets,
    DrainMode,
    DrainObservation,
    DrainOutcome,
    DrainRefusal,
    DrainRequest,
    WorkerNodeId,
    observe_writer_drain,
    wait_for_stable_writer_drain,
)
from app.services.deployment_writer_drain_transport import (
    RedisCeleryDrainSource,
    RedisDrainConfiguration,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def disposable_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, Redis]]:
    url = os.environ.get("TEST_WRITER_DRAIN_REDIS_URL", "")
    parsed = urlsplit(url)
    if (
        os.environ.get("TEST_WRITER_DRAIN_REDIS_DISPOSABLE") != "1"
        or parsed.scheme != "redis"
        or parsed.hostname
        not in {"127.0.0.1", "localhost", "redis", "writer-drain-redis"}
        or parsed.path not in {f"/{db}" for db in range(10, 16)}
        or parsed.query
    ):
        pytest.fail("explicit exclusive hosted disposable Redis DB 10..15 required")
    client = Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    assert client.ping()
    assert client.dbsize() == 0, "disposable drain DB must start empty"
    # Celery 5.4 Settings gives these environment values precedence over
    # Celery(..., broker=url). Root conftest deliberately binds memory://;
    # real canary workers must instead share the observer's exclusive DB.
    # The observer CLI deliberately refuses split-broker overlays, including
    # equal URLs. Clear overlays so workers fall back to the same base URL
    # and the fresh-process CLI canary inherits supported configuration.
    for key in ("CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key in ("CELERY_BROKER_URL", "TEST_BROKER"):
        monkeypatch.setenv(key, url)
    try:
        yield url, client
    finally:
        # Fixture owns this exclusive test DB; never called by observation code.
        client.flushdb()
        client.close()


def broker_request() -> DrainRequest:
    return DrainRequest(DrainMode.BROKER_ONLY, (), DrainBudgets(2, 30, 5))


def source(url: str) -> RedisCeleryDrainSource:
    return RedisCeleryDrainSource(configuration=RedisDrainConfiguration(broker_url=url))


def assert_disposable_worker_broker(*, app: Celery, url: str) -> None:
    assert all(
        effective == url
        for effective in (
            app.conf.broker_url,
            app.conf.broker_read_url,
            app.conf.broker_write_url,
        )
    ), "worker broker settings must resolve to the observer's disposable DB"


def test_actual_empty404_all_priority_queues_and_orphan_unacked(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, client = disposable_broker
    observer = source(url)
    empty = observe_writer_drain(request=broker_request(), source=observer)
    assert empty.outcome is DrainOutcome.OBSERVED_EMPTY
    assert empty.observation is not None
    assert (
        tuple(q.queue.value for q in empty.observation.queues) == DEPLOYMENT_QUEUE_NAMES
    )
    for queue in DEPLOYMENT_QUEUE_NAMES:
        for priority in (0, 3, 6, 9):
            key = queue if priority == 0 else f"{queue}\x06\x16{priority}"
            client.lpush(key, b"test-only-not-customer-data")
    busy = observe_writer_drain(request=broker_request(), source=observer)
    assert busy.outcome is DrainOutcome.OBSERVED_BUSY
    assert busy.observation is not None
    assert all(q.ready == 4 for q in busy.observation.queues)
    for queue in DEPLOYMENT_QUEUE_NAMES:
        for priority in (0, 3, 6, 9):
            client.delete(queue if priority == 0 else f"{queue}\x06\x16{priority}")
    for key, operation in (("unacked", "hash"), ("unacked_index", "index")):
        if operation == "hash":
            client.hset(key, "orphan", "test-only")
        else:
            client.zadd(key, {"orphan": 1})
        assert (
            observe_writer_drain(request=broker_request(), source=observer).outcome
            is DrainOutcome.OBSERVED_BUSY
        )
        client.delete(key)
    assert (
        observe_writer_drain(request=broker_request(), source=observer).outcome
        is DrainOutcome.OBSERVED_EMPTY
    )


def test_configured_unacked_keys_are_observed(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, client = disposable_broker
    observer = RedisCeleryDrainSource(
        configuration=RedisDrainConfiguration(
            broker_url=url,
            unacked_key="custom_unacked",
            unacked_index_key="custom_unacked_index",
        )
    )
    client.hset("custom_unacked", "orphan", "test-only")
    client.zadd("custom_unacked_index", {"orphan": 1})
    result = observe_writer_drain(request=broker_request(), source=observer)
    assert result.outcome is DrainOutcome.OBSERVED_BUSY
    assert result.observation is not None
    assert result.observation.unacknowledged.hash_entries == 1
    assert result.observation.unacknowledged.index_entries == 1


def test_wrongtype_protocol_error_is_never_zero(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, client = disposable_broker
    client.set("billing", "wrong-type")
    result = observe_writer_drain(request=broker_request(), source=source(url))
    assert result.outcome is DrainOutcome.REFUSED
    assert result.refusal is DrainRefusal.TRANSPORT_UNAVAILABLE


def test_actual_broker_observer_performs_only_read_commands(
    disposable_broker: tuple[str, Redis], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _client = disposable_broker
    commands: list[str] = []
    redis_execute_command = Redis.execute_command
    original_pipeline = Pipeline.execute_command

    def client_command(self: Redis, *args: object, **kwargs: object) -> object:
        commands.append(str(args[0]).upper())
        return redis_execute_command(self, *args, **kwargs)

    def pipeline_command(self: Pipeline, *args: object, **kwargs: object) -> object:
        commands.append(str(args[0]).upper())
        return original_pipeline(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Redis, "execute_command", client_command)
        patch.setattr(Pipeline, "execute_command", pipeline_command)
        result = observe_writer_drain(request=broker_request(), source=source(url))
    assert result.outcome is DrainOutcome.OBSERVED_EMPTY
    assert commands
    assert set(commands) <= {"PING", "EXISTS", "LLEN", "HLEN", "ZCARD"}


def test_actual_worker_active_reserved_eta_and_missing_node(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, _client = disposable_broker
    app = Celery("hosted_drain_canary", broker=url, set_as_current=False)
    app.conf.update(
        task_queues=tuple(Queue(q) for q in DEPLOYMENT_QUEUE_NAMES),
        task_acks_late=True,
        worker_prefetch_multiplier=4,
        task_default_queue="billing",
        accept_content=["json"],
        task_serializer="json",
        broker_transport_options={"socket_timeout": 2, "socket_connect_timeout": 2},
    )
    assert_disposable_worker_broker(app=app, url=url)
    started = threading.Event()
    release = threading.Event()

    @app.task(name="hosted_drain_block")
    def block() -> None:
        started.set()
        if not release.wait(30):
            raise RuntimeError("hosted blocking canary deadline")

    @app.task(name="hosted_drain_noop")
    def noop() -> None:
        return None

    try:
        with start_worker(
            app,
            pool="threads",
            concurrency=1,
            perform_ping_check=False,
            shutdown_timeout=10,
        ) as worker:
            request = DrainRequest(
                DrainMode.WORKERS_AND_BROKER,
                (WorkerNodeId(worker.hostname),),
                DrainBudgets(2, 30, 5),
            )
            observer = source(url)
            assert (
                observe_writer_drain(request=request, source=observer).outcome
                is DrainOutcome.OBSERVED_EMPTY
            )
            missing = DrainRequest(
                DrainMode.WORKERS_AND_BROKER,
                (WorkerNodeId(worker.hostname), WorkerNodeId("celery@absent")),
                request.budgets,
            )
            assert (
                observe_writer_drain(request=missing, source=observer).refusal
                is DrainRefusal.MISSING_NODE
            )
            block.delay()
            assert started.wait(5)
            noop.delay()
            noop.apply_async(countdown=60, expires=90)
            # Each raw inspect waits its bounded response window, letting the
            # real consumer reserve queued and ETA tasks before the next command.
            result = observe_writer_drain(request=request, source=observer)
            assert result.outcome is DrainOutcome.OBSERVED_BUSY
            assert result.observation is not None
            population = result.observation.workers[0]
            assert population.active >= 1
            assert population.reserved >= 1
            assert population.scheduled >= 1
            release.set()
    finally:
        release.set()
        app.close()


def test_fresh_process_never_imports_scheduler_or_database(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, _client = disposable_broker
    code = """
import os
import sys
from scripts.observe_writer_drain import main
assert main(['inspect', '--mode', 'broker-only', '--drain-budget', '10']) == 0
assert not {'app.celery_app', 'app.services.scheduler_config', 'app.db', 'app.config'} & sys.modules.keys()
"""
    process = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "CELERY_BROKER_URL": url},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    # Never echo captured transport output into failure messages.
    assert process.returncode == 0
    assert '"mode":"broker-only"' in process.stdout


def test_actual_late_enqueue_resets_zero_pair(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, client = disposable_broker
    observer = source(url)

    class LateEnqueueSource:
        calls: int = 0

        def observe(self, *, request: DrainRequest, timeout: float) -> DrainObservation:
            observed = observer.observe(request=request, timeout=timeout)
            self.calls += 1
            if self.calls == 1:
                client.lpush("billing", "test-only-late-enqueue")
            elif self.calls == 2:
                assert not observed.empty
                client.delete("billing")
            return observed

    late = LateEnqueueSource()
    result = wait_for_stable_writer_drain(request=broker_request(), source=late)
    assert result.outcome is DrainOutcome.STABLE_EMPTY
    assert late.calls == 4


def test_full_inspection_never_restores_expired_financial_unacked(
    disposable_broker: tuple[str, Redis],
) -> None:
    url, client = disposable_broker
    with Connection(url) as incumbent_connection:
        with incumbent_connection.channel() as incumbent_channel:
            Producer(incumbent_channel).publish(
                {"hosted_canary": True},
                routing_key="billing",
                declare=[Queue("billing")],
                serializer="json",
            )
            delivery = incumbent_channel.basic_get(queue="billing", no_ack=False)
            assert delivery is not None
            try:
                before_hash = client.hgetall("unacked")
                assert len(before_hash) == 1
                tag = next(iter(before_hash))
                client.zadd("unacked_index", {tag: 0})
                before_index = client.zrange("unacked_index", 0, -1, withscores=True)
                assert client.llen("billing") == 0
                full = DrainRequest(
                    DrainMode.WORKERS_AND_BROKER,
                    (WorkerNodeId("celery@absent"),),
                    DrainBudgets(2, 30, 5),
                )
                result = observe_writer_drain(request=full, source=source(url))
                assert result.refusal is DrainRefusal.MISSING_NODE
                assert client.hgetall("unacked") == before_hash
                assert (
                    client.zrange("unacked_index", 0, -1, withscores=True)
                    == before_index
                )
                assert client.llen("billing") == 0
            finally:
                # Fixture delivery belongs to this test, never the observer.
                delivery.ack()


def run_isolated_canary_worker(*, node: str, ready: Path) -> None:
    """Child-process entry point; never share Celery's global shutdown state."""
    url = os.environ["TEST_WRITER_DRAIN_REDIS_URL"]

    # start_worker supplies anon_nodename() itself; its controller must replace
    # that keyword to expose the exact node identity used by the real census.
    class NamedWorker(CeleryTestWorkController):
        def __init__(self, *, hostname: str, **kwargs: object) -> None:
            super().__init__(hostname=node, **kwargs)

    with Celery("hosted_extra_worker", broker=url, set_as_current=False) as app:
        app.conf.task_queues = (Queue("billing"),)
        assert_disposable_worker_broker(app=app, url=url)
        with start_worker(
            app, perform_ping_check=False, WorkController=NamedWorker
        ) as worker:
            assert worker.hostname == node
            ready.touch()
            # Parent closes stdin to request normal context-manager cleanup.
            sys.stdin.buffer.read(1)


@contextmanager
def isolated_canary_worker(*, node: str, ready: Path) -> Iterator[WorkerNodeId]:
    # Separate processes avoid nested testing-worker helpers resetting Celery's
    # process-global should_terminate flag while another worker is still exiting.
    code = """
import runpy
import sys
from pathlib import Path
namespace = runpy.run_path(sys.argv[1])
namespace['run_isolated_canary_worker'](node=sys.argv[2], ready=Path(sys.argv[3]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(Path(__file__).resolve()), node, str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert process.poll() is None, "isolated canary worker failed at startup"
            assert time.monotonic() < deadline, "isolated canary worker startup timeout"
            time.sleep(0.05)
        assert process.poll() is None, "isolated canary worker exited after startup"
        yield WorkerNodeId(node)
    finally:
        assert process.stdin is not None
        process.stdin.close()
        forced_cleanup = False
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            forced_cleanup = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert process.poll() is not None, "isolated canary worker was not reaped"
        # Forced cleanup prevents leaks but must never turn a broken fixture
        # into successful acceptance evidence.
        assert not forced_cleanup, "isolated canary worker required forced cleanup"
        assert process.returncode == 0, (
            f"isolated canary worker cleanup failed (exit {process.returncode})"
        )


def test_actual_extra_worker_is_not_hidden_by_destination_filter(
    disposable_broker: tuple[str, Redis], tmp_path: Path
) -> None:
    url, _client = disposable_broker
    with isolated_canary_worker(
        node="incumbent@hosted-drain", ready=tmp_path / "incumbent.ready"
    ) as incumbent:
        with isolated_canary_worker(
            node="extra@hosted-drain", ready=tmp_path / "extra.ready"
        ) as extra:
            assert incumbent != extra
            full = DrainRequest(
                DrainMode.WORKERS_AND_BROKER,
                (incumbent,),
                DrainBudgets(2, 30, 5),
            )
            result = observe_writer_drain(request=full, source=source(url))
            assert result.refusal is DrainRefusal.UNEXPECTED_NODE


def test_actual_control_polling_budget_sensitivity(
    disposable_broker: tuple[str, Redis], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _client = disposable_broker
    full = DrainRequest(
        DrainMode.WORKERS_AND_BROKER,
        (WorkerNodeId("celery@absent"),),
        DrainBudgets(2, 30, 5),
    )
    started = time.monotonic()
    result = observe_writer_drain(request=full, source=source(url))
    assert result.refusal is DrainRefusal.MISSING_NODE
    assert time.monotonic() - started < full.budgets.inspection_timeout

    original_init = RedisTransport.__init__

    def default_polling(self: RedisTransport, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        # Reintroduce the pinned transport's one-second BRPOP and no sleep.
        # The actual Redis canary must distinguish this broken mechanism from
        # a completed census, without enlarging the hard request deadline.
        self.polling_interval = None
        self.brpop_timeout = 1

    with monkeypatch.context() as patch:
        patch.setattr(RedisTransport, "__init__", default_polling)
        broken = observe_writer_drain(request=full, source=source(url))
    assert broken.refusal is DrainRefusal.INSPECTION_TIMEOUT


def test_control_event_flood_and_blocked_cleanup_are_deadline_bounded(
    disposable_broker: tuple[str, Redis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, _client = disposable_broker
    original_exit = Connection.__exit__
    events = 0
    cleanup_started = False

    def flooded_event(self: Connection, **kwargs: object) -> None:
        nonlocal events
        events += 1
        # Pidbox must not turn infinitely arriving events into an unbounded
        # request, even when every individual event is immediately available.

    def blocked_exit(self: Connection, *exc_info: object) -> None:
        nonlocal cleanup_started
        cleanup_started = True
        time.sleep(10)
        original_exit(self, *exc_info)

    full = DrainRequest(
        DrainMode.WORKERS_AND_BROKER,
        (WorkerNodeId("celery@absent"),),
        DrainBudgets(0.1, 10, 5),
    )
    started = time.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(Connection, "drain_events", flooded_event)
        patch.setattr(Connection, "__exit__", blocked_exit)
        result = observe_writer_drain(request=full, source=source(url), timeout=0.3)
    assert result.refusal is DrainRefusal.INSPECTION_TIMEOUT
    assert events > 1
    assert cleanup_started
    assert time.monotonic() - started < 1.5
