"""Process-local scope to suppress customer notifications during back-office work.

Some operations move money or status around as *bookkeeping catch-up* rather than
in response to a real-time customer action — e.g. reconciling already-arrived
funds onto the right invoice, or a bulk credit reallocation. Those must not fire
"Payment received" / "Invoice paid" / "Service resumed" emails: the customer did
not just pay, the activity is old, and a burst of such mail to a mostly-churned
cohort (canceled/disabled/hidden mailboxes) is factually wrong and burns sender
reputation.

Wrap such work in ``suppress_notifications()``; the ``NotificationHandler``
checks ``notifications_suppressed()`` and skips queueing. Event dispatch is
synchronous and inline (see ``emit_event`` → ``dispatcher.dispatch``), so the
ContextVar set here is visible to the handler running in the same call stack.
Only customer-facing notifications are suppressed — webhooks, audit/event-store,
and enforcement side-effects still run.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_suppressed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "notifications_suppressed", default=False
)


def notifications_suppressed() -> bool:
    """True when the current context is inside a ``suppress_notifications`` scope."""
    return _suppressed.get()


@contextmanager
def suppress_notifications() -> Iterator[None]:
    """Suppress customer notifications for the duration of the block."""
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)
