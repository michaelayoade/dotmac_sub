"""Provisioning handler for event-driven automation."""

import logging

from sqlalchemy.orm import Session

from app.models.provisioning import ProvisioningRun, ProvisioningRunStatus, ServiceOrder
from app.schemas.provisioning import ProvisioningRunStart
from app.services import provisioning as provisioning_service
from app.services.common import coerce_uuid
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


class ProvisioningHandler:
    """Handler that triggers provisioning workflows on key events."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.subscription_activated:
            self._handle_subscription_activated(db, event)
        elif event.event_type == EventType.service_order_assigned:
            self._handle_service_order_assigned(db, event)

    def _handle_subscription_activated(self, db: Session, event: Event) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.warning(
                "Skipping auto IP allocation: missing subscription_id in event payload."
            )
            return
        # Step 1: Allocate IP addresses
        try:
            provisioning_service.ensure_ip_assignments_for_subscription(
                db, str(subscription_id)
            )
        except Exception as exc:
            logger.warning(
                "Auto IP allocation failed for subscription %s: %s",
                subscription_id,
                exc,
            )
        # Step 2: Sync RADIUS credentials so subscriber can authenticate
        self._sync_radius_on_activation(db, str(subscription_id))
        # Step 3: Push NAS provisioning commands
        self._push_nas_provisioning(db, str(subscription_id))

    def _sync_radius_on_activation(self, db: Session, subscription_id: str) -> None:
        """Sync RADIUS credentials for the subscription's subscriber."""
        try:
            from app.models.catalog import Subscription
            subscription = db.get(Subscription, coerce_uuid(subscription_id))
            if not subscription:
                return
            from app.services.radius import sync_account_credentials_to_radius
            count = sync_account_credentials_to_radius(db, str(subscription.subscriber_id))
            if count:
                logger.info(
                    "Synced %d RADIUS credentials for subscription %s activation.",
                    count, subscription_id,
                )
        except Exception as exc:
            logger.warning(
                "RADIUS credential sync failed for subscription %s: %s",
                subscription_id, exc,
            )

    def _push_nas_provisioning(self, db: Session, subscription_id: str) -> None:
        """Push NAS provisioning commands on subscription activation."""
        try:
            from app.models.catalog import NasDevice, Subscription
            from app.services.connection_type_provisioning import (
                build_nas_provisioning_commands,
            )
            from app.services.enforcement import _resolve_effective_profile
            from app.services.nas import DeviceProvisioner

            subscription = db.get(Subscription, coerce_uuid(subscription_id))
            if not subscription or not subscription.provisioning_nas_device_id:
                return
            nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
            if not nas_device:
                return
            profile = _resolve_effective_profile(db, subscription)
            commands = build_nas_provisioning_commands(
                db, subscription, nas_device, profile=profile, action="create",
            )
            if not commands:
                return
            for cmd in commands:
                try:
                    DeviceProvisioner._execute_ssh(nas_device, cmd)
                except Exception as cmd_exc:
                    logger.warning(
                        "NAS command failed for subscription %s: %s (cmd: %s)",
                        subscription_id, cmd_exc, cmd,
                    )
            logger.info(
                "Pushed %d NAS provisioning commands for subscription %s.",
                len(commands), subscription_id,
            )
        except Exception as exc:
            logger.warning(
                "NAS provisioning failed for subscription %s: %s",
                subscription_id, exc,
            )

    def _handle_service_order_assigned(self, db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get("service_order_id")
        if not service_order_id:
            logger.warning(
                "Skipping provisioning run: missing service_order_id in event payload."
            )
            return
        try:
            order_uuid = coerce_uuid(service_order_id)
        except (TypeError, ValueError):
            logger.warning("Skipping provisioning run: invalid service_order_id.")
            return
        service_order = db.get(ServiceOrder, order_uuid)
        if not service_order:
            logger.warning(
                "Skipping provisioning run: service order %s not found.",
                service_order_id,
            )
            return
        existing = (
            db.query(ProvisioningRun)
            .filter(ProvisioningRun.service_order_id == service_order.id)
            .filter(ProvisioningRun.status != ProvisioningRunStatus.failed)
            .first()
        )
        if existing:
            logger.info(
                "Skipping provisioning run for service order %s: existing run %s with status %s.",
                service_order_id,
                existing.id,
                existing.status.value,
            )
            return
        workflow = provisioning_service.resolve_workflow_for_service_order(db, service_order)
        if not workflow:
            logger.warning(
                "Skipping provisioning run for service order %s: no active workflow found.",
                service_order_id,
            )
            return
        try:
            provisioning_service.provisioning_runs.run(
                db,
                str(workflow.id),
                ProvisioningRunStart(
                    service_order_id=service_order.id,
                    subscription_id=service_order.subscription_id,
                ),
            )
        except Exception as exc:
            logger.exception(
                "Provisioning run failed for service order %s: %s",
                service_order_id,
                exc,
            )
