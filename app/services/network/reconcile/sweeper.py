"""Periodic sweeper — long-running process, NOT a Celery task.

Walks every active ``OntUnit`` row at a fixed interval and runs
``reconcile_ont(mode="sweep")`` against each. The sweep mode proceeds even
against ``out_of_sync`` rows (this is how they clear), so the sweeper is
the primary mechanism for self-healing drift detected post-write.

Design rules (from the original architecture discussion):

* **No queue, no retry machinery.** A reconcile either converges or marks
  the ONT ``out_of_sync``. The sweeper picks it up on the next pass.
* **Reachability fast-fail.** Before spending an SSH/NBI roundtrip per
  ONT, the sweeper does a ~100ms mgmt-IP ping. If the ONT is unreachable
  the sweeper increments ``consecutive_sweep_unreachable`` on the row and
  skips the detailed reconcile. After N consecutive unreachable sweeps
  the operator gets an alert (Phase 2 — alert escalation isn't wired in
  this commit, just the counter).
* **Process, not Celery.** Single instance, deterministic, no per-task
  queue depth to debug. Deploys as a systemd-managed process alongside
  the FastAPI app.
* **Bounded per-ONT timeout.** Each reconcile gets a hard ceiling
  (default 60s — same as the sync HTTP path) so one unhealthy device
  doesn't block the rest of the sweep.

This module exposes ``SweepLoop`` + ``run_sweep_once`` so the same logic
can run as a daemon (``run_forever``), a one-shot test pass
(``run_once``), or a CLI invocation (``scripts/run_sweeper.py``).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network.reconcile.readers.reachability import (
    PingFunction,
    is_pingable,
)

from . import reconcile_ont
from .adapters import desired_from_ont_unit

logger = logging.getLogger(__name__)


# ── Result shape ────────────────────────────────────────────────────────────


@dataclass
class SweepStats:
    """Roll-up of one sweep pass — emitted to logs and metrics."""

    started_at: datetime
    completed_at: datetime | None = None
    total_onts: int = 0
    reconciled: int = 0
    skipped_unreachable: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds()


# ── Per-ONT pass ───────────────────────────────────────────────────────────


def _sweep_one(
    db: Session,
    ont_id,
    *,
    timeout_sec: int,
    ping_function: PingFunction | None,
    reconcile_fn: Callable = reconcile_ont,
) -> tuple[bool, bool]:
    """Reconcile one ONT in sweep mode. Returns ``(reachable, success)``.

    Resolves the desired state to read the mgmt IP for the reachability
    check. If the ONT is unreachable, the per-ONT
    ``consecutive_sweep_unreachable`` counter is incremented and the
    function returns ``(False, False)`` — no SSH/NBI roundtrips fired.

    On reachable ONTs, runs ``reconcile_ont(mode="sweep")`` and returns
    ``(True, result.success)``.
    """
    ont = db.execute(
        select(OntUnit).where(OntUnit.id == ont_id)
    ).scalar_one_or_none()
    if ont is None:
        return False, False

    # Cheap pre-flight: ping the mgmt IP. We resolve desired_state just for
    # the IP — not the full reconcile path.
    desired = desired_from_ont_unit(db, ont)
    reachable = is_pingable(desired.mgmt_ip, ping_function=ping_function)
    if not reachable:
        ont.consecutive_sweep_unreachable = (
            (ont.consecutive_sweep_unreachable or 0) + 1
        )
        ont.last_reconciled_at = datetime.now(UTC)
        return False, False

    result = reconcile_fn(
        db,
        ont.id,
        proposed_change=None,
        mode="sweep",
        timeout_sec=timeout_sec,
        ping_function=ping_function,
    )
    return True, result.success


def run_sweep_once(
    db_factory: Callable[[], Session],
    *,
    timeout_sec: int = 60,
    ping_function: PingFunction | None = None,
    reconcile_fn: Callable = reconcile_ont,
    only_active: bool = True,
) -> SweepStats:
    """Sweep every active ONT once and return aggregated stats.

    ``db_factory`` is called per-ONT to get a fresh session — sweeps run
    long enough that holding a single session for the whole pass risks
    DB connection timeouts.
    """
    started = datetime.now(UTC)
    stats = SweepStats(started_at=started)

    # First pass: collect target IDs (with a short-lived session).
    with db_factory() as catalog_db:
        stmt = select(OntUnit.id)
        if only_active:
            stmt = stmt.where(OntUnit.is_active.is_(True))
        ont_ids = [row[0] for row in catalog_db.execute(stmt).all()]

    stats.total_onts = len(ont_ids)
    logger.info(
        "sweep_cycle_begin",
        extra={"total_onts": stats.total_onts, "started_at": started.isoformat()},
    )

    for ont_id in ont_ids:
        try:
            with db_factory() as ont_db:
                reachable, success = _sweep_one(
                    ont_db,
                    ont_id,
                    timeout_sec=timeout_sec,
                    ping_function=ping_function,
                    reconcile_fn=reconcile_fn,
                )
                ont_db.commit()
        except Exception as exc:  # noqa: BLE001 — defensive per-ONT
            stats.errors.append(f"{ont_id}: {exc}")
            logger.exception(
                "sweep_per_ont_error",
                extra={"ont_id": str(ont_id), "error": str(exc)},
            )
            continue

        if not reachable:
            stats.skipped_unreachable += 1
            continue
        stats.reconciled += 1
        if success:
            stats.succeeded += 1
        else:
            stats.failed += 1

    stats.completed_at = datetime.now(UTC)
    logger.info(
        "sweep_cycle_complete",
        extra={
            "total_onts": stats.total_onts,
            "reconciled": stats.reconciled,
            "skipped_unreachable": stats.skipped_unreachable,
            "succeeded": stats.succeeded,
            "failed": stats.failed,
            "errors": len(stats.errors),
            "duration_sec": stats.duration_sec,
        },
    )
    return stats


# ── Long-running loop ──────────────────────────────────────────────────────


class SweepLoop:
    """Runs ``run_sweep_once`` on a fixed interval until stopped.

    Designed for systemd-managed deployment alongside the FastAPI app.
    Single-process, single-thread — no need to coordinate with Celery
    workers. Use ``stop()`` for clean shutdown; the loop respects
    SIGTERM/SIGINT when installed via ``install_signal_handlers``.
    """

    def __init__(
        self,
        db_factory: Callable[[], Session],
        *,
        interval_sec: int = 4 * 3600,  # 4h default
        timeout_sec: int = 60,
        ping_function: PingFunction | None = None,
    ):
        self._db_factory = db_factory
        self._interval = interval_sec
        self._timeout = timeout_sec
        self._ping_function = ping_function
        self._stop = threading.Event()

    def stop(self) -> None:
        """Request a clean shutdown after the current cycle completes."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to ``stop()``. Call this from the daemon
        entry-point; tests shouldn't call it."""
        signal.signal(signal.SIGTERM, lambda *a: self.stop())
        signal.signal(signal.SIGINT, lambda *a: self.stop())

    def run_forever(self) -> None:
        logger.info(
            "sweep_loop_starting",
            extra={"interval_sec": self._interval, "timeout_sec": self._timeout},
        )
        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                run_sweep_once(
                    self._db_factory,
                    timeout_sec=self._timeout,
                    ping_function=self._ping_function,
                )
            except Exception:
                logger.exception("sweep_cycle_failed")

            # Sleep the remainder of the interval, in small wake-ups so
            # ``stop()`` from a signal handler takes effect within ~1s.
            elapsed = time.monotonic() - cycle_started
            sleep_remaining = max(0.0, self._interval - elapsed)
            deadline = time.monotonic() + sleep_remaining
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(min(1.0, deadline - time.monotonic()))

        logger.info("sweep_loop_stopped")


__all__ = ("SweepLoop", "SweepStats", "run_sweep_once")
