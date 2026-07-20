"""Deterministic repro + regression tests for DbScheduler under-firing.

Drives the real celery 5.4 ``Scheduler.tick`` heap machinery through
``DbScheduler`` with a simulated clock (no threads, no broker, no sleeping):
``app.now``/``crontab.nowfun``/``time.monotonic`` all read one FakeClock, and
the drive loop advances the clock by exactly what ``tick()`` asks for, the
same way ``celery.beat.Service.start`` sleeps.

Prod incident (2026-07-04): with beat continuously up, a 5s interval task
fired on time all day while ``warm_topology_status`` (180s timedelta) was
delivered only ~8-9 times in 3-6h and hourly timedelta rows never fired.
The mechanism: ``build_beat_schedule()`` swallows mid-build DB errors and
returns a partial dict; ``DbScheduler._refresh_schedule`` treated that as
authoritative, popping the missing keys and re-creating them on the next
refresh as brand-new entries with ``last_run_at = now`` — restarting every
interval countdown. Keys whose interval exceeds the gap between such resets
never come due; crontab entries are immune (wall-clock anchored), which is
why PR #777's hourly anchoring sidestepped it for hourly+ tasks.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from celery import Celery
from celery.schedules import crontab

import app.celery_scheduler as celery_scheduler_module
from app.celery_scheduler import DbScheduler

REFRESH_SECONDS = 30
MAX_INTERVAL = 5


class FakeClock:
    def __init__(self):
        self.start = datetime(2026, 7, 1, 20, 22, 47, tzinfo=UTC)
        self.epoch = 0.0

    @property
    def dt(self) -> datetime:
        return self.start + timedelta(seconds=self.epoch)

    def now(self) -> datetime:
        return self.dt

    def monotonic(self) -> float:
        return self.epoch

    def advance(self, seconds: float) -> None:
        self.epoch += seconds


def _spec(name: str, kind: str, value, clock: FakeClock) -> dict:
    if kind == "td":
        schedule = timedelta(seconds=value)
    else:
        minute, hour = value
        # Same bound method every build so crontab specs compare equal across
        # refreshes, like prod's nowfun=None (BaseSchedule.__eq__ checks it).
        schedule = crontab(minute=minute, hour=hour, nowfun=clock.now)
    return {
        "task": f"app.tasks.fake.{name}",
        "schedule": schedule,
        "args": [],
        "kwargs": {},
        "options": {},
    }


def _prod_like_specs() -> tuple[dict, dict]:
    """~60 entries mimicking prod: stable head + DB-row tail.

    head: entries built before the ScheduledTask row query (GIS/CRM style)
    tail: the scheduled_task_* rows appended last by build_beat_schedule —
          the ones a mid-build failure drops wholesale.
    """
    head = {"warm_5s": ("td", 5)}
    for i in range(20):
        head[f"cron_hourly_{i}"] = ("cron", (i * 3 % 60, "*/1"))
    for i in range(6):
        head[f"cron_daily_{i}"] = ("cron", (i * 11 % 60, str(i % 6)))
    tail = {
        "warm_topology_status": ("td", 180),
        "hourly_td_row": ("td", 3600),
    }
    for i, secs in enumerate([300, 600, 900, 1800, 120, 240, 60] * 4):
        tail[f"row_{i}"] = ("td", secs)
    return head, tail


def _simulate(build, sim_seconds: float, clock: FakeClock):
    """Run DbScheduler against `build` for `sim_seconds` of simulated time."""
    app = Celery(set_as_current=False)
    app.conf.beat_max_loop_interval = MAX_INTERVAL
    app.conf.beat_refresh_seconds = REFRESH_SECONDS
    app.now = clock.now

    fires: Counter = Counter()
    fire_times: dict[str, list[float]] = {}
    populates = [0]

    class RecordingScheduler(DbScheduler):
        producer = None  # shadow the cached_property; never touch a broker

        def apply_entry(self, entry, producer=None):
            fires[entry.name] += 1
            fire_times.setdefault(entry.name, []).append(clock.epoch)

        def populate_heap(self, *args, **kwargs):
            populates[0] += 1
            return super().populate_heap(*args, **kwargs)

    with (
        mock.patch.object(celery_scheduler_module, "build_beat_schedule", build),
        mock.patch.object(
            celery_scheduler_module,
            "time",
            SimpleNamespace(monotonic=clock.monotonic),
        ),
    ):
        scheduler = RecordingScheduler(
            app=app,
            schedule={},
            max_interval=MAX_INTERVAL,
            Producer=lambda *args, **kwargs: None,
        )
        ticks = 0
        while clock.epoch < sim_seconds:
            delay = scheduler.tick()
            ticks += 1
            # Mirror Service.start: sleep for the returned interval, or loop
            # immediately (with a tiny epsilon of loop cost) when it's 0.
            clock.advance(delay if delay and delay > 0 else 0.01)
            assert ticks < 1_000_000, "runaway tick loop"
    return fires, fire_times, populates[0]


def test_intermittent_partial_builds_do_not_starve_sub_hourly_entries():
    """THE REPRO. Every 3rd build drops the scheduled_task tail (transient
    DB error semantics of build_beat_schedule). Over 2 simulated hours the
    180s entry must still fire ~40 times; on the pre-fix scheduler it fires
    exactly 0 times while the 5s entry stays healthy (the prod signature).
    """
    clock = FakeClock()
    head, tail = _prod_like_specs()
    build_calls = [0]

    def build():
        build_calls[0] += 1
        out = {k: _spec(k, kind, v, clock) for k, (kind, v) in head.items()}
        if build_calls[0] % 3 == 0:
            return out  # partial build: tail rows missing this refresh
        out.update({k: _spec(k, kind, v, clock) for k, (kind, v) in tail.items()})
        return out

    fires, _, _ = _simulate(build, sim_seconds=2 * 3600, clock=clock)

    # 5s entry: healthy in both broken and fixed schedulers (~1435 fires).
    assert fires["warm_5s"] >= 1400
    # 180s entry: ~40 expected; each fire may slip by <= one refresh while its
    # key is absent. Broken scheduler: 0.
    assert fires["warm_topology_status"] >= 34
    # 120s rows (row_4, row_11, ...): ~60 expected. Broken scheduler: 0.
    assert fires["row_4"] >= 50
    # Hourly timedelta row: due at t=3600 and t=7200; at least the first
    # must fire even if the boundary lands in an absence window.
    assert fires["hourly_td_row"] >= 1


def test_add_and_remove_rows_propagate_within_a_refresh():
    clock = FakeClock()
    head, tail = _prod_like_specs()

    def build():
        out = {k: _spec(k, kind, v, clock) for k, (kind, v) in head.items()}
        current_tail = dict(tail)
        if clock.epoch >= 1000:
            current_tail["added_row"] = ("td", 120)
        if clock.epoch >= 6000:
            current_tail.pop("row_2", None)  # a 900s row
        out.update(
            {k: _spec(k, kind, v, clock) for k, (kind, v) in current_tail.items()}
        )
        return out

    fires, fire_times, _ = _simulate(build, sim_seconds=2 * 3600, clock=clock)

    # Added at t>=1000, picked up by the next refresh (<=1030), first fire one
    # interval later (<=1150) plus scheduling slack.
    added = fire_times.get("added_row", [])
    assert added, "added row never fired"
    assert added[0] <= 1000 + REFRESH_SECONDS + 120 + 2 * MAX_INTERVAL
    assert len(added) >= 48  # ~51 expected over the remaining ~6200s
    # Removed at t>=6000: nothing may fire after the removal refresh.
    late_row_2 = [t for t in fire_times.get("row_2", []) if t > 6000 + REFRESH_SECONDS]
    assert late_row_2 == []


def test_interval_change_takes_effect_within_a_refresh():
    """Shrinking a row's interval (3600s -> 120s) must re-time its heap event.

    Pre-fix, the in-place ScheduleEntry.update was invisible to
    schedules_equal (old_schedulers aliases the same object), so the entry
    kept its stale pre-change heap event and first fired at t~3600.
    """
    clock = FakeClock()
    head, tail = _prod_like_specs()

    def build():
        out = {k: _spec(k, kind, v, clock) for k, (kind, v) in head.items()}
        current_tail = dict(tail)
        if clock.epoch >= 300:
            current_tail["hourly_td_row"] = ("td", 120)
        out.update(
            {k: _spec(k, kind, v, clock) for k, (kind, v) in current_tail.items()}
        )
        return out

    fires, fire_times, _ = _simulate(build, sim_seconds=3600, clock=clock)

    changed = fire_times.get("hourly_td_row", [])
    assert changed, "changed row never fired"
    # Change lands at the t=300+ refresh (<=330); entry was created at t=0 so
    # under the 120s schedule it is immediately due once the heap is rebuilt.
    assert changed[0] <= 300 + REFRESH_SECONDS + 2 * MAX_INTERVAL
    assert fires["hourly_td_row"] >= 25  # ~27 expected at 120s cadence


def test_steady_state_refresh_does_not_disturb_entries_or_rebuild_heap():
    """No churn -> no heap rebuilds, no entry resets, exact fire counts."""
    clock = FakeClock()
    head, tail = _prod_like_specs()

    def build():
        out = {k: _spec(k, kind, v, clock) for k, (kind, v) in head.items()}
        out.update({k: _spec(k, kind, v, clock) for k, (kind, v) in tail.items()})
        return out

    fires, _, populate_calls = _simulate(build, sim_seconds=3600, clock=clock)

    assert fires["warm_5s"] >= 700  # ~717
    assert fires["warm_topology_status"] in (19, 20)
    assert fires["row_4"] in (29, 30)  # 120s row
    assert fires["cron_hourly_0"] >= 1
    # The heap must not be rebuilt on every refresh when nothing changed
    # (initial populate + at most a couple of snapshot-related rebuilds).
    assert populate_calls <= 3
