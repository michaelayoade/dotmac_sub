"""Thin durable-event adapter for test expiry and connectivity projection."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services.events.handlers.owner_session import owner_session
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.test_connection import (
    EXPIRY_TRIGGER,
    OWNER,
    ExpireTestConnectionCommand,
    RecordTestConnectionDeliveryCommand,
    TestConnectionNetworkQuery,
    expire_test_connection,
    reconcile_test_connection_network,
    record_delivery,
)

HANDLED_EVENT_TYPES = frozenset(
    {EventType.custom, EventType.subscription_test_connection_changed}
)


class TestConnectionHandler:
    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        context = CommandContext.system(
            actor=OWNER,
            scope=OWNER,
            reason="Apply current test-access consequence",
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
        )
        if event.event_type is EventType.custom:
            if (
                event.payload.get("trigger") != EXPIRY_TRIGGER
                or event.payload.get("timer_owner") != OWNER
            ):
                return
            grant_id = UUID(str(event.payload["entity_id"]))
            with owner_session(db) as owner_db:
                expire_test_connection(
                    owner_db,
                    command=ExpireTestConnectionCommand(
                        context=context, grant_id=grant_id
                    ),
                )
            return
        if event.payload.get("schema_version") != 1 or event.subscription_id is None:
            raise ValueError("Invalid test-connection event envelope")
        grant_id = UUID(str(event.payload["grant_id"]))
        try:
            outcome = reconcile_test_connection_network(
                db,
                query=TestConnectionNetworkQuery(
                    subscription_id=event.subscription_id, grant_id=grant_id
                ),
            )
        except Exception as exc:
            with owner_session(db) as owner_db:
                record_delivery(
                    owner_db,
                    command=RecordTestConnectionDeliveryCommand(
                        context=context,
                        grant_id=grant_id,
                        applied=False,
                        error_code=type(exc).__name__,
                    ),
                )
            raise
        if outcome.active_grant_id == grant_id:
            with owner_session(db) as owner_db:
                record_delivery(
                    owner_db,
                    command=RecordTestConnectionDeliveryCommand(
                        context=context, grant_id=grant_id, applied=True
                    ),
                )
