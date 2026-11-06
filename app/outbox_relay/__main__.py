"""Entry point: ``python -m app.outbox_relay``.

Reads only ``OUTBOX_RELAY_*`` variables. It deliberately does not import
``app.config`` / ``app.db`` so the process cannot pick up ``DATABASE_URL`` or
any application credential.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import uuid
from types import FrameType

from dotmac_kernel.messaging.relay import RelayPolicy
from prometheus_client import start_http_server
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.outbox_relay.config import RelayConfig, RelayConfigError
from app.outbox_relay.dispatcher import run_forever
from app.outbox_relay.health import write_heartbeat
from app.outbox_relay.metrics import OldestPendingTracker
from app.outbox_relay.preflight import (
    dispatcher_identity_violations,
    observe_dispatcher,
)
from app.outbox_relay.registry import build_default_registry

EXIT_REFUSED = 2


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    log = logging.getLogger("app.outbox_relay")
    try:
        config = RelayConfig.from_env(os.environ)
    except RelayConfigError as exc:
        log.error("outbox relay refused to start: %s", exc)
        return EXIT_REFUSED

    engine = create_engine(
        config.database_url, pool_size=1, max_overflow=0, pool_pre_ping=True
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as probe:
        violations = dispatcher_identity_violations(observe_dispatcher(probe))
        probe.rollback()
    if violations:
        for violation in violations:
            log.error("outbox relay refused to start: %s", violation)
        return EXIT_REFUSED

    registry = build_default_registry()
    tenant_factory = None
    if config.tenant_database_url:
        tenant_engine = create_engine(
            config.tenant_database_url, pool_size=1, max_overflow=0, pool_pre_ping=True
        )
        tenant_factory = sessionmaker(bind=tenant_engine, expire_on_commit=False)
    elif registry.event_types:
        log.error(
            "outbox relay refused to start: handlers are registered but "
            "OUTBOX_RELAY_TENANT_DATABASE_URL is not set"
        )
        return EXIT_REFUSED
    policy = RelayPolicy(
        batch_size=config.batch_size,
        max_attempts=config.max_attempts,
        stale_lease_seconds=config.stale_lease_seconds,
    )
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received; stopping after the current cycle", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start_http_server(config.metrics_port)
    log.info(
        "outbox relay up: worker=%s handlers=%d tenant plane only",
        worker_id,
        len(registry.event_types),
    )
    run_forever(
        session_factory=factory,
        tenant_session_factory=tenant_factory,
        registry=registry,
        policy=policy,
        worker_id=worker_id,
        park_seconds=config.park_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
        tracker=OldestPendingTracker(retain_seconds=config.park_seconds * 2.0),
        stop=stop,
        heartbeat=lambda: write_heartbeat(config.heartbeat_path),
    )
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
