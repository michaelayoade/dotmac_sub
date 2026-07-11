"""Event handler: relay vendor-relevant project stubs to CRM (Phase 3, risk #6).

Consumes the sub-native project lifecycle events (``EventType.custom`` with
``payload["name"] == "project.created"``/… — the events ``Projects.create`` /
``Projects.update`` already emit, risk #13) and, behind the write-flip flag,
enqueues a stub push into CRM ``projects`` so vendor-portal / dotmac_field
``installation_projects`` creation keeps resolving its FK until the Phase 5
vendor port.

Decoupled by design: the projects service emits its lifecycle events unchanged;
this handler is the only relay trigger. Gates on the flag + vendor-relevance
here (so the ~90% non-vendor project events never enqueue a task); the task
re-checks both defensively.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.project import Project
from app.services.events.types import Event, EventType
from app.services.vendor_project_relay import (
    RELAY_EVENT_NAMES,
    coerce_uuid,
    enqueue_project_relay,
    is_vendor_relevant,
    relay_enabled,
)

logger = logging.getLogger(__name__)


class VendorProjectRelayHandler:
    """Enqueue a CRM project-stub relay for vendor-relevant project changes."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type is not EventType.custom:
            return
        name = event.payload.get("name")
        if name not in RELAY_EVENT_NAMES:
            return
        try:
            self._dispatch(db, event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vendor project relay handler failed for %s: %s", name, exc)

    def _dispatch(self, db: Session, event: Event) -> None:
        from app.config import settings

        # No CRM configured → nothing to relay (and nothing to retry).
        if not settings.crm_base_url:
            return
        if not relay_enabled(db):
            return

        project_id = coerce_uuid(event.payload.get("project_id"))
        if project_id is None:
            return
        project = db.get(Project, project_id)
        if project is None or not is_vendor_relevant(project):
            return

        enqueue_project_relay(project.id, source="vendor_project_relay_handler")
