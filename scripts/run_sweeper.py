#!/usr/bin/env python3
"""Entry point for the periodic reconciler sweeper.

Deployed via systemd alongside the FastAPI app — see
``deploy/systemd/dotmac-reconcile-sweeper.service``. Single-process,
single-instance.

Environment overrides:

* ``RECONCILE_SWEEP_INTERVAL_SEC`` — sweep cadence (default 14400 = 4h).
* ``RECONCILE_SWEEP_TIMEOUT_SEC`` — per-ONT reconcile cap (default 60).
"""

from __future__ import annotations

import logging
import os

from app.db import SessionLocal
from app.services.network.reconcile.sweeper import SweepLoop

logger = logging.getLogger(__name__)


def _db_factory():
    return SessionLocal()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("RECONCILE_SWEEP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    interval = int(os.getenv("RECONCILE_SWEEP_INTERVAL_SEC", "14400"))
    timeout = int(os.getenv("RECONCILE_SWEEP_TIMEOUT_SEC", "60"))

    loop = SweepLoop(
        db_factory=_db_factory,
        interval_sec=interval,
        timeout_sec=timeout,
    )
    loop.install_signal_handlers()
    logger.info("reconcile_sweeper_starting", extra={"pid": os.getpid()})
    loop.run_forever()


if __name__ == "__main__":
    main()
