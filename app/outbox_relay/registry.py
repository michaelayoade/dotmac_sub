"""Event-type handler registry with a fail-safe unregistered rule.

An event whose type has no registered handler is never failed or dead-lettered
by the relay: the dispatcher parks it (back to ``pending`` with a later
``available_at``, attempts unchanged) so it is picked up once a handler is
registered. Dead-lettering is reserved for a registered handler that keeps
raising.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from dotmac_kernel.messaging.relay import ClaimedEvent


class OutboxEventHandler(Protocol):
    """Delivers one claimed event; raises to request retry/dead-letter.

    A handler receives the claimed event only. It gets no tenant session: a
    handler that must read tenant data needs its own reviewed tenant-scoped
    connection design, which no composed module requires today.
    """

    def __call__(self, event: ClaimedEvent) -> None: ...


class DuplicateHandlerError(ValueError):
    """An event type already has a handler; one owner per event type."""


class OutboxHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, OutboxEventHandler] = {}

    def register(self, event_type: str, handler: OutboxEventHandler) -> None:
        normalized = event_type.strip()
        if not normalized:
            raise ValueError("event_type must be non-empty")
        if normalized in self._handlers:
            raise DuplicateHandlerError(
                f"event type {normalized!r} already has a handler"
            )
        self._handlers[normalized] = handler

    def get(self, event_type: str) -> OutboxEventHandler | None:
        return self._handlers.get(event_type)

    @property
    def event_types(self) -> Mapping[str, OutboxEventHandler]:
        return dict(self._handlers)


def build_default_registry() -> OutboxHandlerRegistry:
    """The shipped registry is EMPTY.

    No composed module has a Sub-side consumer yet, so every event parks. A
    consumer registers here in the same change that introduces it.
    """
    return OutboxHandlerRegistry()
