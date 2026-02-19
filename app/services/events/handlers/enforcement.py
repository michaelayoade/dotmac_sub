"""Event-driven enforcement for sessions and FUP actions."""

import logging

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.enforcement import (
    apply_radius_profile_to_account,
    apply_subscription_address_list_block,
    disconnect_account_sessions,
    disconnect_subscription_sessions,
    remove_subscription_address_list_block,
)
from app.services.events import emit_event
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


class EnforcementHandler:
    """Handler that applies session enforcement based on events."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.subscription_suspended:
            self._handle_subscription_block(db, event, "suspended")
        elif event.event_type == EventType.subscription_canceled:
            self._handle_subscription_block(db, event, "canceled")
        elif event.event_type == EventType.subscription_activated:
            self._handle_subscription_restore(db, event)
        elif event.event_type == EventType.subscription_resumed:
            self._handle_subscription_restore(db, event)
        elif event.event_type == EventType.subscriber_throttled:
            self._handle_account_throttle(db, event)
        elif event.event_type == EventType.usage_exhausted:
            self._handle_usage_exhausted(db, event)

    def _handle_subscription_block(self, db: Session, event: Event, reason: str) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.warning("Skipping session disconnect: missing subscription_id.")
            return
        try:
            disconnect_subscription_sessions(db, str(subscription_id), reason=reason)
            apply_subscription_address_list_block(db, str(subscription_id))
        except Exception as exc:
            logger.warning(
                "Failed to disconnect sessions for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_subscription_restore(self, db: Session, event: Event) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            return
        refresh = settings_spec.resolve_value(
            db, SettingDomain.radius, "refresh_sessions_on_profile_change"
        )
        refresh_enabled = str(refresh).lower() not in {"0", "false", "no", "off"}
        try:
            if refresh_enabled:
                disconnect_subscription_sessions(db, str(subscription_id), reason="restore")
            remove_subscription_address_list_block(db, str(subscription_id))
        except Exception as exc:
            logger.warning(
                "Failed to refresh sessions for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_account_throttle(self, db: Session, event: Event) -> None:
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            logger.warning("Skipping throttle enforcement: missing account_id.")
            return
        refresh = settings_spec.resolve_value(
            db, SettingDomain.radius, "refresh_sessions_on_profile_change"
        )
        refresh_enabled = str(refresh).lower() not in {"0", "false", "no", "off"}
        try:
            if refresh_enabled:
                disconnect_account_sessions(db, str(account_id), reason="throttle")
        except Exception as exc:
            logger.warning(
                "Failed to disconnect sessions for account %s: %s",
                account_id,
                exc,
            )

    def _handle_usage_exhausted(self, db: Session, event: Event) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        account_id = event.account_id or event.payload.get("account_id")
        if not subscription_id or not account_id:
            logger.warning("Skipping FUP enforcement: missing subscription/account.")
            return
        action = settings_spec.resolve_value(db, SettingDomain.usage, "fup_action") or "throttle"
        if action not in {"throttle", "suspend", "block", "none"}:
            action = "throttle"
        if action == "none":
            return
        if action == "block":
            try:
                disconnect_subscription_sessions(db, str(subscription_id), reason="fup_block")
                apply_subscription_address_list_block(db, str(subscription_id))
            except Exception as exc:
                logger.warning(
                    "Failed to apply FUP block for subscription %s: %s",
                    subscription_id,
                    exc,
                )
            return
        if action == "suspend":
            subscription = db.get(Subscription, subscription_id)
            if not subscription:
                return
            if subscription.status == SubscriptionStatus.active:
                subscription.status = SubscriptionStatus.suspended
                db.flush()  # Use flush, not commit - let dispatcher manage transaction
                emit_event(
                    db,
                    EventType.subscription_suspended,
                    {
                        "subscription_id": str(subscription.id),
                        "from_status": "active",
                        "to_status": "suspended",
                        "reason": "fup_exhausted",
                    },
                    subscription_id=subscription.id,
                    account_id=subscription.subscriber_id,
                )
            return
        throttle_profile_id = settings_spec.resolve_value(
            db, SettingDomain.usage, "fup_throttle_radius_profile_id"
        )
        if not throttle_profile_id:
            logger.warning("FUP throttle profile not configured.")
            return
        try:
            updated = apply_radius_profile_to_account(
                db, str(account_id), str(throttle_profile_id)
            )
            if updated:
                refresh = settings_spec.resolve_value(
                    db, SettingDomain.radius, "refresh_sessions_on_profile_change"
                )
                refresh_enabled = str(refresh).lower() not in {"0", "false", "no", "off"}
                if refresh_enabled:
                    disconnect_account_sessions(db, str(account_id), reason="fup_throttle")
        except Exception as exc:
            logger.warning(
                "Failed to apply FUP throttle for subscription %s: %s",
                subscription_id,
                exc,
            )
