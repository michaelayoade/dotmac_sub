"""Multi-process proof of the per-OLT SSH session limiter against PostgreSQL.

The limiter's whole point is that the cap holds ACROSS processes (Celery
workers, the web app, operator scripts), so these tests spawn real OS
processes that contend for slots through the CI PostgreSQL service. They need a
disposable ``TEST_DATABASE_URL`` database and run in the PostgreSQL integration
shards (``make test-integration-shard``). The limiter only uses advisory locks
(no tables), so it leaves nothing behind; each test uses a fresh random OLT key.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.services.network.olt_ssh_limiter import (
    OltSessionBusy,
    OltSshPriority,
    acquire_slot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("olt_limiter_worker.py")


@pytest.fixture
def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("multi-process limiter proof needs TEST_DATABASE_URL (PostgreSQL)")
    return url


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


@pytest.fixture
def procs():
    started: list[subprocess.Popen] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _spawn(procs, database_url, log_path, olt_key, **kwargs) -> subprocess.Popen:
    args = {
        "database_url": database_url,
        "olt_key": olt_key,
        "log": str(log_path),
        "limit": 2,
        "reserved": 1,
        "priority": "WRITE",
        "wait_seconds": 30,
        "hold_seconds": 1.0,
        **kwargs,
    }
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, str(WORKER), json.dumps(args)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    procs.append(proc)
    return proc


def _events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


def _wait_for(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


def _peak_concurrency(events: list[dict]) -> int:
    timeline = sorted(
        (e["t_ns"], 1 if e["event"] == "acquired" else -1)
        for e in events
        if e["event"] in {"acquired", "released"}
    )
    # Releases sort before acquires at the same instant (conservative).
    timeline.sort(key=lambda item: (item[0], item[1]))
    peak = current = 0
    for _t, delta in timeline:
        current += delta
        peak = max(peak, current)
    return peak


def _probe(database_url: str, olt_key: str, priority=OltSshPriority.WRITE):
    return acquire_slot(
        olt_key=olt_key,
        limit=2,
        reserved=1,
        priority=priority,
        wait_seconds=0,
        database_url=database_url,
    )


def test_six_processes_never_hold_more_than_two_slots(procs, database_url, log_path):
    olt_key = f"mp-{uuid.uuid4()}"
    start_at_ns = time.time_ns() + 20_000_000_000  # all six race at one instant
    workers = [
        _spawn(
            procs,
            database_url,
            log_path,
            olt_key,
            label=f"w{i}",
            hold_seconds=2.0,
            wait_seconds=90,
            start_at_ns=start_at_ns,
        )
        for i in range(6)
    ]
    for proc in workers:
        assert proc.wait(timeout=120) == 0, proc.stderr.read().decode()[-2000:]

    events = _events(log_path)
    acquired = [e for e in events if e["event"] == "acquired"]
    assert len(acquired) == 6, "every writer must eventually get a slot"
    assert len({e["pid"] for e in acquired}) == 6, "six distinct OS processes"
    assert _peak_concurrency(events) == 2, (
        "peak concurrently held slots must equal the limit (2): never more, "
        "and the limit is actually used"
    )
    # Nothing leaked: both slots are free again.
    first, second = _probe(database_url, olt_key), _probe(database_url, olt_key)
    first.release()
    second.release()


def test_sigkilled_holder_frees_its_slot_within_five_seconds(
    procs, database_url, log_path
):
    olt_key = f"mp-{uuid.uuid4()}"
    holders = [
        _spawn(
            procs,
            database_url,
            log_path,
            olt_key,
            label=f"h{i}",
            hold_seconds=120,
            wait_seconds=0,
        )
        for i in range(2)
    ]
    _wait_for(
        lambda: sum(e["event"] == "acquired" for e in _events(log_path)) == 2,
        timeout=60,
        what="both holders to take a slot",
    )
    with pytest.raises(OltSessionBusy):
        _probe(database_url, olt_key)

    holders[0].send_signal(signal.SIGKILL)
    holders[0].wait(timeout=10)
    started = time.monotonic()
    freed = None
    while time.monotonic() - started < 5.0:
        try:
            freed = _probe(database_url, olt_key)
            break
        except OltSessionBusy:
            time.sleep(0.1)
    assert freed is not None, "a SIGKILLed holder's slot was not freed within 5 s"
    freed.release()


def test_background_never_holds_reserved_slot_and_skips_while_writer_waits(
    procs, database_url, log_path
):
    olt_key = f"mp-{uuid.uuid4()}"

    # A background holder takes the one unreserved slot (index 0).
    _spawn(
        procs,
        database_url,
        log_path,
        olt_key,
        label="bg1",
        priority="BACKGROUND",
        wait_seconds=0,
        hold_seconds=120,
    )
    _wait_for(
        lambda: any(
            e["label"] == "bg1" and e["event"] == "acquired" for e in _events(log_path)
        ),
        timeout=60,
        what="first background holder",
    )
    bg1 = next(e for e in _events(log_path) if e["label"] == "bg1")
    assert bg1["slot"] == 0

    # A second background caller is refused although the reserved slot is free.
    second = _spawn(
        procs,
        database_url,
        log_path,
        olt_key,
        label="bg2",
        priority="BACKGROUND",
        wait_seconds=0,
        hold_seconds=1,
    )
    assert second.wait(timeout=60) == 0
    assert any(e["label"] == "bg2" and e["event"] == "busy" for e in _events(log_path))
    assert not any(
        e["label"] == "bg2" and e["event"] == "acquired" for e in _events(log_path)
    )

    # A writer takes the reserved slot (index 1); a second writer then waits.
    _spawn(
        procs,
        database_url,
        log_path,
        olt_key,
        label="w1",
        priority="WRITE",
        wait_seconds=0,
        hold_seconds=120,
    )
    _wait_for(
        lambda: any(
            e["label"] == "w1" and e["event"] == "acquired" for e in _events(log_path)
        ),
        timeout=60,
        what="first writer",
    )
    assert next(e for e in _events(log_path) if e["label"] == "w1")["slot"] == 1

    waiting_writer = _spawn(
        procs,
        database_url,
        log_path,
        olt_key,
        label="w2",
        priority="WRITE",
        wait_seconds=60,
        hold_seconds=1,
    )
    # Give w2 time to register its intent on the lock server, then a background
    # caller must skip with the higher-priority reason (even if a slot were free).
    _wait_for(lambda: waiting_writer.poll() is None, timeout=5, what="w2 running")
    time.sleep(3.0)
    third = _spawn(
        procs,
        database_url,
        log_path,
        olt_key,
        label="bg3",
        priority="BACKGROUND",
        wait_seconds=0,
        hold_seconds=1,
    )
    assert third.wait(timeout=60) == 0
    busy = [
        e for e in _events(log_path) if e["label"] == "bg3" and e["event"] == "busy"
    ]
    assert busy, "background must skip while a writer is queued"
    assert "olt_busy:" in busy[0]["message"]

    for proc in list(procs):
        if proc.poll() is None:
            proc.kill()
