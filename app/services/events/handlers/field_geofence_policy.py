"""Deliver committed position evidence to Sub's geofence policy owner."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services.events.owner_outputs import require_output_text
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset({EventType.position_observation_recorded})


class FieldGeofencePolicyHandler:
    """Thin durable-event adapter; policy remains in the geofence service."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        observation_id = require_output_text(
            event.payload,
            "observation_id",
            consumer="operations.field_geofence_policy",
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        from app.services.field import geofence

        geofence.consume_position_observation(db, UUID(observation_id))


__all__ = ["HANDLED_EVENT_TYPES", "FieldGeofencePolicyHandler"]
