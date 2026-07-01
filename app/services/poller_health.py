"""Cross-process bandwidth-poller health snapshot.

The poller runs as its own process, so Prometheus metrics set there are never
scraped by the web ``/metrics`` endpoint (no multiprocess mode). The poller
writes a small JSON health snapshot to Redis each cycle and the web-process
collector (``app.metrics._PollerHealthCollector``) reads it on scrape.

Kept dependency-light (just redis + json) so ``app.metrics`` can import it
without pulling in the poller module's heavy RouterOS deps.
"""

from __future__ import annotations

import json
import os
from typing import Any

POLLER_HEALTH_KEY = os.getenv("BANDWIDTH_POLLER_HEALTH_KEY", "bandwidth:poller:health")

_redis_client: Any = None


def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        import redis

        # Cached per process; short timeouts so a scrape never blocks.
        _redis_client = redis.Redis.from_url(
            url, socket_timeout=2, socket_connect_timeout=2
        )
    return _redis_client


def load_poller_health() -> dict[str, Any] | None:
    """Latest poller health snapshot, or None. Never raises (scrape path)."""
    try:
        client = _get_redis()
        if client is None:
            return None
        raw = client.get(POLLER_HEALTH_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None
