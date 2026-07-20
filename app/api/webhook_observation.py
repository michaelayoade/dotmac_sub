"""Uniform webhook receipt/latency/outcome recording for inbound webhook routes.

One context manager so every webhook route reports the same three-outcome shape
(docs/designs/CHANNEL_OBSERVABILITY.md) without each route hand-rolling timing
and try/except. An HTTP error — a failed signature check or a malformed body —
is the provider's fault or a rejection, not ours, so it counts as ``rejected``;
any other exception is a genuine processing fault and counts as ``error``. A
clean exit counts as ``accepted``. Latency is recorded for every outcome that
got past the point of raising, so a slow-then-rejected call is still visible.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from starlette.exceptions import HTTPException

from app.metrics import observe_webhook_event


@contextmanager
def webhook_observation(*, provider: str, event: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    except HTTPException:
        observe_webhook_event(
            provider=provider,
            event=event,
            outcome="rejected",
            duration_seconds=time.monotonic() - started,
        )
        raise
    except Exception:
        observe_webhook_event(
            provider=provider,
            event=event,
            outcome="error",
            duration_seconds=time.monotonic() - started,
        )
        raise
    else:
        observe_webhook_event(
            provider=provider,
            event=event,
            outcome="accepted",
            duration_seconds=time.monotonic() - started,
        )
