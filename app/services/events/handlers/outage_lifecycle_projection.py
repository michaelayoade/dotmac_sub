"""Project committed outage lifecycle facts into downstream owners.

The handler is deliberately orchestration-only: incident transitions remain
facts owned by ``network.outage_lifecycle``, while this adapter asks the
operational-escalation owner to apply the consequence — attach operational
owners/watchers and plan SLA escalations when an incident becomes
customer-visible, and cancel escalations when it terminates. Detection and
recovery remain observation loops; outage resolution never closes support
Tickets or WorkOrders (Support and Field owners transition their own cases
from recovery evidence).

A consequence that cannot be applied raises so the event delivery stays
failed and retryable instead of a warning log.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.outage_created,
        EventType.outage_confirmed,
        EventType.outage_discarded,
        EventType.outage_resolved,
    }
)


class OutageLifecycleProjectionHandler:
    """Request idempotent downstream outage consequences after commit."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type in {
            EventType.outage_created,
            EventType.outage_confirmed,
        }:
            self._apply_activation_consequences(db, event)
        elif event.event_type in {
            EventType.outage_discarded,
            EventType.outage_resolved,
        }:
            self._cancel_escalations(db, event)

    @staticmethod
    def _incident(db: Session, event: Event):
        from app.models.network_monitoring import OutageIncident
        from app.services.common import coerce_uuid

        incident_id = event.payload.get("incident_id")
        if not incident_id:
            logger.warning(
                "outage lifecycle event %s has no incident id", event.event_id
            )
            return None
        return db.get(OutageIncident, coerce_uuid(incident_id))

    def _apply_activation_consequences(self, db: Session, event: Event) -> None:
        from app.services.topology.outage import CLASSIFIER_TERMINAL_STATUSES
        from app.services.topology.outage_operations import (
            ensure_outage_customer_watchers,
            ensure_outage_operations,
            plan_outage_escalations,
        )

        incident = self._incident(db, event)
        if incident is None:
            return
        # A stale replay after the incident already terminated must not plan
        # fresh escalations that nothing would cancel.
        if incident.status in CLASSIFIER_TERMINAL_STATUSES:
            return
        ensure_outage_operations(db, incident)
        ensure_outage_customer_watchers(db, incident)
        plan_outage_escalations(db, incident, trigger=event.event_type.value)

    def _cancel_escalations(self, db: Session, event: Event) -> None:
        from app.models.operational_escalation import OperationalEntityType
        from app.services import operational_escalation

        incident = self._incident(db, event)
        if incident is None:
            return
        canceled_at: datetime | None = None
        resolved_at = event.payload.get("resolved_at")
        if resolved_at:
            canceled_at = datetime.fromisoformat(resolved_at)
        operational_escalation.cancel_entity_events(
            db,
            entity_type=OperationalEntityType.outage,
            entity_id=incident.id,
            reason=event.event_type.value,
            canceled_at=canceled_at,
        )
