"""Event handler that pushes subscriber/subscription changes to DotMac Omni CRM."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

# Events that should trigger CRM sync
CRM_SYNC_EVENTS = {
    EventType.subscriber_created,
    EventType.subscriber_suspended,
    EventType.subscriber_reactivated,
    EventType.subscription_activated,
    EventType.subscription_suspended,
    EventType.subscription_resumed,
    EventType.subscription_canceled,
    EventType.subscription_upgraded,
    EventType.subscription_downgraded,
}


class CrmSyncHandler:
    """Push subscriber status and service changes to DotMac Omni CRM."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in CRM_SYNC_EVENTS:
            return

        try:
            self._dispatch(db, event)
        except Exception as exc:
            logger.warning(
                "CRM sync failed for event %s: %s",
                event.event_type.value,
                exc,
            )

    def _dispatch(self, db: Session, event: Event) -> None:
        from app.config import settings
        from app.services.crm_webhook import (
            SELFCARE_EXTERNAL_SYSTEM,
            native_status,
            native_subscriber_payload,
            service_activation_payload,
            status_change_payload,
        )

        # No CRM configured → nothing to push (and nothing to retry).
        if not settings.crm_base_url:
            return

        # Resolve subscriber and external identity
        subscriber_id = event.account_id or event.payload.get("account_id")
        if not subscriber_id:
            return

        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            return

        # Every subscriber — migrated or native — is pushed to the CRM under its
        # canonical selfcare keying, keyed by our subscriber UUID, so the CRM
        # updates one canonical row. Previously migrated subscribers were pushed
        # under "splynx" by splynx_customer_id, which created a duplicate row
        # alongside the selfcare row for the same subscriber.
        splynx_id = subscriber.splynx_customer_id
        is_native = splynx_id is None
        external_system = SELFCARE_EXTERNAL_SYSTEM
        external_id: int | str = str(subscriber.id)
        name = f"{subscriber.first_name} {subscriber.last_name}".strip()

        if event.event_type == EventType.subscriber_created:
            # Make the subscriber exist in the CRM (tickets, agent views).
            # Migrated subscribers are already there via the bulk sync.
            if is_native:
                self._enqueue(
                    external_id,
                    native_subscriber_payload(subscriber),
                    event,
                    external_system,
                )
            return

        if event.event_type in (
            EventType.subscriber_suspended,
            EventType.subscriber_reactivated,
        ):
            status = event.payload.get("to_status") or (
                "blocked"
                if event.event_type == EventType.subscriber_suspended
                else "active"
            )
            payload = (
                native_subscriber_payload(subscriber, status=native_status(status))
                if is_native
                else status_change_payload(status, name)
            )
            self._enqueue(external_id, payload, event, external_system)

        elif event.event_type in (
            EventType.subscription_activated,
            EventType.subscription_suspended,
            EventType.subscription_resumed,
            EventType.subscription_canceled,
            EventType.subscription_upgraded,
            EventType.subscription_downgraded,
        ):
            subscription_id = event.subscription_id or event.payload.get(
                "subscription_id"
            )
            if not subscription_id:
                return
            subscription = db.get(Subscription, subscription_id)
            service_name = ""
            service_speed = ""
            if subscription and subscription.offer:
                service_name = subscription.offer.name
                down = subscription.offer.speed_download_mbps
                up = subscription.offer.speed_upload_mbps
                if down and up:
                    service_speed = f"{down}/{up} Mbps"

            status_map = {
                EventType.subscription_activated: "active",
                EventType.subscription_resumed: "active",
                EventType.subscription_suspended: "blocked",
                EventType.subscription_canceled: "disabled",
                EventType.subscription_upgraded: "active",
                EventType.subscription_downgraded: "active",
            }
            status = status_map.get(event.event_type, "active")
            payload = (
                native_subscriber_payload(
                    subscriber,
                    service_name=service_name,
                    service_speed=service_speed,
                    status=native_status(status),
                )
                if is_native
                else service_activation_payload(service_name, service_speed, status)
            )
            self._enqueue(external_id, payload, event, external_system)

    def _enqueue(
        self,
        external_id: int | str,
        payload: dict,
        event: Event,
        external_system: str = "splynx",
    ) -> None:
        """Queue the outbound CRM push so it never blocks the request thread."""
        from app.services.queue_adapter import enqueue_task
        from app.tasks.crm_sync import push_subscriber_change

        enqueue_task(
            push_subscriber_change,
            args=[external_id, payload, external_system],
            correlation_id=f"crm_sync:{event.event_id}",
            source="crm_sync_handler",
        )
