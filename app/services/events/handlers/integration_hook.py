"""Integration hook handler for the event system."""

from sqlalchemy.orm import Session

from app.services import integration_hooks as integration_hooks_service
from app.services.events.types import Event


class IntegrationHookHandler:
    """Dispatches emitted events to configured integration hooks."""

    def handle(self, db: Session, event: Event) -> None:
        integration_hooks_service.dispatch_for_event(
            db,
            event_type=event.event_type.value,
            payload=event.to_dict(),
        )

