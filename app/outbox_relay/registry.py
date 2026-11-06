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
from sqlalchemy.orm import Session


class OutboxEventHandler(Protocol):
    """Delivers one claimed event; raises to request retry/dead-letter.

    Same shape as the kernel's ``DeliveryTransport.deliver(event, tenant_db)``:
    ``tenant_db`` is a session on the tenant-scoped (RLS) credential with
    ``app.current_tenant`` restored to ``event.tenant_id``. The dispatcher
    credential never reads tenant data. The relay commits ``tenant_db`` after a
    clean return and rolls it back on an exception.
    """

    def __call__(self, event: ClaimedEvent, tenant_db: Session) -> None: ...


class DuplicateHandlerError(ValueError):
    """An event type already has a handler; one owner per event type."""


class OutboxHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, OutboxEventHandler] = {}
        self._acknowledged: set[str] = set()

    def register(self, event_type: str, handler: OutboxEventHandler) -> None:
        normalized = event_type.strip()
        if not normalized:
            raise ValueError("event_type must be non-empty")
        if normalized in self._handlers or normalized in self._acknowledged:
            raise DuplicateHandlerError(
                f"event type {normalized!r} already has a handler"
            )
        self._handlers[normalized] = handler

    def acknowledge_without_consumer(self, event_type: str) -> None:
        """Declare a type Sub deliberately does not consume: it settles as
        ``sent`` instead of parking forever. An explicit, reviewed entry."""
        normalized = event_type.strip()
        if not normalized:
            raise ValueError("event_type must be non-empty")
        if normalized in self._handlers or normalized in self._acknowledged:
            raise DuplicateHandlerError(
                f"event type {normalized!r} is already registered"
            )
        self._acknowledged.add(normalized)

    def is_acknowledged(self, event_type: str) -> bool:
        return event_type in self._acknowledged

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
