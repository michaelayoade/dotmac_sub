"""Event-driven enforcement for sessions and FUP actions."""

import logging

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.models.subscriber import SubscriberStatus as AccountStatus
from app.services import radius as radius_service
from app.services import radius_reject as radius_reject_service
from app.services import settings_spec
from app.services.enforcement import (
    apply_radius_profile_to_account,
    apply_subscription_address_list_block,
    disconnect_account_sessions,
    disconnect_subscription_sessions,
    remove_subscription_address_list_block,
    update_subscription_sessions,
)
from app.services.events import emit_event
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


def _reject_reason_from_event_payload(payload: dict) -> str:
    raw = str((payload or {}).get("reason") or "").strip().lower()
    if raw in {"dunning", "negative_balance", "negative-balance"}:
        return "negative"
    return "blocked"


class EnforcementHandler:
    """Handler that applies session enforcement based on events."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.subscription_suspended:
            self._handle_subscription_block(db, event, "suspended")
        elif event.event_type == EventType.subscription_canceled:
            self._handle_subscription_cancel(db, event)
        elif event.event_type == EventType.subscription_expired:
            self._handle_subscription_block(db, event, "expired")
        elif event.event_type == EventType.subscription_activated:
            self._handle_subscription_restore(db, event)
        elif event.event_type == EventType.subscription_resumed:
            self._handle_subscription_restore(db, event)
        elif event.event_type in (
            EventType.subscription_upgraded,
            EventType.subscription_downgraded,
        ):
            self._handle_subscription_speed_change(db, event)
        elif event.event_type == EventType.subscriber_throttled:
            self._handle_account_throttle(db, event)
        elif event.event_type == EventType.usage_exhausted:
            self._handle_usage_exhausted(db, event)
        elif event.event_type == EventType.payment_received:
            self._handle_payment_received(db, event)
        elif event.event_type == EventType.invoice_overdue:
            self._handle_invoice_overdue(db, event)

    def _enforce_subscription_block(
        self,
        db: Session,
        subscription_id: str,
        *,
        reason: str = "suspended",
        reject_reason: str = "blocked",
    ) -> None:
        """Apply RADIUS reject, remove credentials, disconnect sessions, and
        add address-list block for a single subscription.  Callable both from
        the event-driven path and directly after ``emit=False`` lifecycle calls.
        Each step is individually guarded so one failure does not prevent the
        remaining enforcement actions."""
        subscription = db.get(Subscription, subscription_id)

        # RADIUS reject IP
        try:
            ip_result = radius_reject_service.enforce_subscription_reject_ip(
                db, str(subscription_id), reject_reason=reject_reason
            )
            if ip_result.get("ok"):
                radius_service.reconcile_subscription_connectivity(
                    db, str(subscription_id)
                )
        except Exception as exc:
            logger.error(
                "Failed to apply RADIUS reject for subscription %s: %s",
                subscription_id,
                exc,
            )

        # Remove external RADIUS credentials
        if subscription:
            try:
                radius_service.remove_external_radius_credentials(
                    db, str(subscription.subscriber_id)
                )
            except Exception as exc:
                logger.error(
                    "Failed to remove RADIUS credentials for subscriber %s: %s",
                    subscription.subscriber_id,
                    exc,
                )

        # Disconnect sessions and apply address list block
        try:
            disconnect_subscription_sessions(db, str(subscription_id), reason=reason)
            apply_subscription_address_list_block(db, str(subscription_id))
        except Exception as exc:
            logger.error(
                "Failed to disconnect sessions for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_subscription_block(
        self, db: Session, event: Event, reason: str
    ) -> None:
        from app.services.account_lifecycle import compute_account_status

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.debug("Skipping session disconnect: event missing subscription_id")
            return

        subscription = db.get(Subscription, subscription_id)

        # Recompute account status (defensive — lifecycle already called this,
        # but non-migrated callers may emit events without lifecycle).
        if subscription:
            try:
                compute_account_status(db, str(subscription.subscriber_id))
            except ValueError:
                logger.error(
                    "Subscriber not found for subscription %s", subscription_id
                )
            except Exception as exc:
                logger.error(
                    "Failed to recompute account status for subscription %s: %s",
                    subscription_id,
                    exc,
                )

        reject_reason = _reject_reason_from_event_payload(event.payload)
        self._enforce_subscription_block(
            db,
            str(subscription_id),
            reason=reason,
            reject_reason=reject_reason,
        )

    def _handle_subscription_cancel(self, db: Session, event: Event) -> None:
        self._handle_subscription_block(db, event, "canceled")

    def _handle_subscription_restore(self, db: Session, event: Event) -> None:
        from app.services.account_lifecycle import compute_account_status

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            return
        refresh = settings_spec.resolve_value(
            db, SettingDomain.radius, "refresh_sessions_on_profile_change"
        )
        refresh_enabled = str(refresh).lower() not in {"0", "false", "no", "off"}

        subscription = db.get(Subscription, subscription_id)

        # Recompute account status
        if subscription:
            try:
                compute_account_status(db, str(subscription.subscriber_id))
            except ValueError:
                logger.error(
                    "Subscriber not found for subscription %s", subscription_id
                )
            except Exception as exc:
                logger.error(
                    "Failed to recompute account status for subscription %s: %s",
                    subscription_id,
                    exc,
                )

        # Clear RADIUS reject and reconcile connectivity
        try:
            ip_result = radius_reject_service.enforce_subscription_reject_ip(
                db, str(subscription_id)
            )
            if ip_result.get("ok"):
                radius_service.reconcile_subscription_connectivity(
                    db, str(subscription_id)
                )
        except Exception as exc:
            logger.error(
                "Failed to clear RADIUS reject for subscription %s: %s",
                subscription_id,
                exc,
            )

        # Refresh sessions and remove address block
        try:
            if refresh_enabled:
                disconnect_subscription_sessions(
                    db, str(subscription_id), reason="restore"
                )
            remove_subscription_address_list_block(db, str(subscription_id))
        except Exception as exc:
            logger.error(
                "Failed to restore sessions for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_subscription_speed_change(self, db: Session, event: Event) -> None:
        """Handle mid-session speed change via CoA-Update."""
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.warning(
                "Skipping speed change enforcement: missing subscription_id."
            )
            return
        try:
            updated = update_subscription_sessions(
                db,
                str(subscription_id),
                reason=event.event_type.value,
            )
            logger.info(
                "Speed change enforcement: %s sessions updated for subscription %s.",
                updated,
                subscription_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to apply speed change for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_account_throttle(self, db: Session, event: Event) -> None:
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            logger.debug("Skipping throttle enforcement: event missing account_id")
            return
        refresh = settings_spec.resolve_value(
            db, SettingDomain.radius, "refresh_sessions_on_profile_change"
        )
        refresh_enabled = str(refresh).lower() not in {"0", "false", "no", "off"}
        try:
            if refresh_enabled:
                disconnect_account_sessions(db, str(account_id), reason="throttle")
        except Exception as exc:
            logger.error(
                "Failed to disconnect sessions for account %s: %s",
                account_id,
                exc,
            )

    def _handle_usage_exhausted(self, db: Session, event: Event) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        account_id = event.account_id or event.payload.get("account_id")
        if not subscription_id or not account_id:
            logger.warning(
                "Skipping FUP enforcement: event missing subscription_id=%s account_id=%s",
                subscription_id,
                account_id,
            )
            return
        action = (
            settings_spec.resolve_value(db, SettingDomain.usage, "fup_action")
            or "throttle"
        )
        if action not in {"throttle", "suspend", "block", "none"}:
            action = "throttle"
        if action == "none":
            return

        # Resolve offer_id and rule_id from payload for state tracking
        offer_id = event.payload.get("offer_id")
        rule_id = event.payload.get("rule_id")
        cap_resets_at_raw = event.payload.get("cap_resets_at")

        if action == "block":
            try:
                disconnect_subscription_sessions(
                    db, str(subscription_id), reason="fup_block"
                )
                apply_subscription_address_list_block(db, str(subscription_id))
                self._persist_fup_state(
                    db,
                    str(subscription_id),
                    offer_id,
                    rule_id,
                    action_status="blocked",
                    cap_resets_at=cap_resets_at_raw,
                    notes="FUP block applied",
                )
            except Exception as exc:
                logger.warning(
                    "Failed to apply FUP block for subscription %s: %s",
                    subscription_id,
                    exc,
                )
            return
        if action == "suspend":
            from app.models.enforcement_lock import EnforcementReason
            from app.services.account_lifecycle import suspend_subscription

            fup_source = f"fup_rule:{rule_id}" if rule_id else "fup_exhausted"
            try:
                suspend_subscription(
                    db,
                    str(subscription_id),
                    reason=EnforcementReason.fup,
                    source=fup_source,
                    emit=False,  # prevent re-entrant dispatch
                )
                # Apply RADIUS enforcement directly (emit=False skips the
                # event-driven path that would normally do this).
                self._enforce_subscription_block(
                    db, str(subscription_id), reason="fup_suspend"
                )
                self._persist_fup_state(
                    db,
                    str(subscription_id),
                    offer_id,
                    rule_id,
                    action_status="blocked",
                    cap_resets_at=cap_resets_at_raw,
                    notes="FUP suspension applied",
                )
            except ValueError as e:
                logger.info(
                    "Skipped FUP suspension for subscription %s: %s",
                    subscription_id,
                    e,
                )
            except Exception as exc:
                logger.error(
                    "Failed to apply FUP suspension for subscription %s: %s",
                    subscription_id,
                    exc,
                )
            return
        throttle_profile_id = settings_spec.resolve_value(
            db, SettingDomain.usage, "fup_throttle_radius_profile_id"
        )
        if not throttle_profile_id:
            logger.warning(
                "FUP throttle profile not configured. "
                "Set 'fup_throttle_radius_profile_id' in usage domain settings."
            )
            return
        try:
            updated = apply_radius_profile_to_account(
                db, str(account_id), str(throttle_profile_id)
            )
            if updated:
                refresh = settings_spec.resolve_value(
                    db, SettingDomain.radius, "refresh_sessions_on_profile_change"
                )
                refresh_enabled = str(refresh).lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
                if refresh_enabled:
                    disconnect_account_sessions(
                        db, str(account_id), reason="fup_throttle"
                    )
                self._persist_fup_state(
                    db,
                    str(subscription_id),
                    offer_id,
                    rule_id,
                    action_status="throttled",
                    throttle_profile_id=str(throttle_profile_id),
                    cap_resets_at=cap_resets_at_raw,
                    notes="FUP throttle applied",
                )
        except Exception as exc:
            logger.warning(
                "Failed to apply FUP throttle for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _persist_fup_state(
        self,
        db: Session,
        subscription_id: str,
        offer_id: str | None,
        rule_id: str | None,
        *,
        action_status: str,
        throttle_profile_id: str | None = None,
        cap_resets_at: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Persist FUP enforcement state for restart resilience."""
        if not offer_id:
            # Try to resolve from subscription
            subscription = db.get(Subscription, subscription_id)
            if subscription:
                offer_id = str(subscription.offer_id) if subscription.offer_id else None
        if not offer_id:
            logger.debug(
                "Cannot persist FUP state: subscription %s has no offer_id (direct plan?)",
                subscription_id,
            )
            return
        try:
            from app.models.fup_state import FupActionStatus
            from app.services.fup_state import fup_state

            status_map = {
                "none": FupActionStatus.none,
                "throttled": FupActionStatus.throttled,
                "blocked": FupActionStatus.blocked,
                "notified": FupActionStatus.notified,
            }
            parsed_resets_at = None
            if cap_resets_at:
                from datetime import datetime

                try:
                    parsed_resets_at = datetime.fromisoformat(cap_resets_at)
                except (ValueError, TypeError):
                    pass

            fup_state.apply_action(
                db,
                subscription_id,
                offer_id=offer_id,
                rule_id=rule_id,
                action_status=status_map.get(action_status, FupActionStatus.none),
                throttle_profile_id=throttle_profile_id,
                cap_resets_at=parsed_resets_at,
                notes=notes,
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist FUP state for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_payment_received(self, db: Session, event: Event) -> None:
        """Auto-reactivate suspended accounts when a payment is received."""
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            return
        try:
            from app.services import collections as collections_service

            invoice_id = event.payload.get("invoice_id")
            restored = collections_service.restore_account_services(
                db,
                str(account_id),
                invoice_id=str(invoice_id) if invoice_id else None,
            )
            if restored:
                logger.info(
                    "Auto-restored %d subscription(s) for account %s after payment",
                    restored,
                    account_id,
                )
        except Exception as exc:
            logger.error(
                "Failed to auto-restore account %s after payment: %s",
                account_id,
                exc,
            )

    def _handle_invoice_overdue(self, db: Session, event: Event) -> None:
        """Auto-suspend subscriber when invoice is overdue, with grace period warning."""
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            return
        try:
            # Check if auto-suspension on overdue is enabled
            enabled = settings_spec.resolve_value(
                db, SettingDomain.billing, "auto_suspend_on_overdue"
            )
            if str(enabled).lower() in {"0", "false", "no", "off", ""}:
                return

            subscriber = db.get(Subscriber, account_id)
            if not subscriber or subscriber.status != AccountStatus.active:
                return

            # Grace period: send warning first, suspend after N hours
            grace_setting = settings_spec.resolve_value(
                db, SettingDomain.billing, "suspension_grace_hours"
            )
            grace_hours = int(str(grace_setting or 48))

            # Check if invoice just became overdue (within grace period)
            invoice_id = event.invoice_id or event.payload.get("invoice_id")
            if invoice_id and grace_hours > 0:
                from app.models.billing import Invoice

                invoice = db.get(Invoice, invoice_id)
                if invoice and invoice.due_at:
                    from datetime import UTC, datetime

                    due_aware = invoice.due_at
                    if due_aware.tzinfo is None:
                        due_aware = due_aware.replace(tzinfo=UTC)
                    hours_overdue = (
                        datetime.now(UTC) - due_aware
                    ).total_seconds() / 3600

                    if hours_overdue < grace_hours:
                        metadata = dict(invoice.metadata_ or {})
                        if metadata.get("suspension_warning_sent_at"):
                            return
                        # Within grace period — emit warning, don't suspend yet
                        emit_event(
                            db,
                            EventType.subscription_suspension_warning,
                            {
                                "invoice_id": str(invoice.id),
                                "invoice_number": invoice.invoice_number or "",
                                "amount": str(invoice.total or 0),
                                "grace_hours": str(grace_hours),
                                "reason": "invoice_overdue",
                            },
                            account_id=subscriber.id,
                        )
                        metadata["suspension_warning_sent_at"] = datetime.now(
                            UTC
                        ).isoformat()
                        invoice.metadata_ = metadata
                        db.flush()
                        logger.info(
                            "Sent suspension warning for account %s (%.1f hrs overdue, grace=%d hrs)",
                            account_id,
                            hours_overdue,
                            grace_hours,
                        )
                        return

            # Past grace period — suspend via lifecycle enforcement locks.
            # Use emit=False to prevent re-entrant event dispatch (this handler
            # already handles the enforcement side effects directly).
            from app.models.enforcement_lock import EnforcementReason
            from app.services.account_lifecycle import (
                compute_account_status,
                suspend_subscription,
            )

            invoice_source = (
                f"invoice:{invoice_id}" if invoice_id else "invoice_overdue"
            )
            # Include suspended subscriptions so overdue locks are created
            # even when a subscription is already suspended by another reason
            # (e.g., FUP). The lock won't change status but tracks the debt.
            subscriptions = (
                db.query(Subscription)
                .filter(
                    Subscription.subscriber_id == subscriber.id,
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.active,
                            SubscriptionStatus.suspended,
                        ]
                    ),
                )
                .all()
            )
            lock_count = 0
            newly_suspended_ids: list[str] = []
            for sub in subscriptions:
                was_active = sub.status == SubscriptionStatus.active
                try:
                    suspend_subscription(
                        db,
                        str(sub.id),
                        reason=EnforcementReason.overdue,
                        source=invoice_source,
                        emit=False,
                    )
                    lock_count += 1
                    # Only apply RADIUS enforcement for subs that were
                    # actually active — already-suspended subs are already
                    # blocked at the network level.
                    if was_active:
                        newly_suspended_ids.append(str(sub.id))
                except ValueError as e:
                    logger.info("Skipped suspending subscription %s: %s", sub.id, e)
                except Exception as exc:
                    logger.error(
                        "Failed to suspend subscription %s for overdue invoice: %s",
                        sub.id,
                        exc,
                    )

            if lock_count:
                compute_account_status(db, str(subscriber.id))
                # Apply RADIUS enforcement only for newly suspended subs
                # (already-suspended subs are already blocked).
                for sid in newly_suspended_ids:
                    self._enforce_subscription_block(
                        db, sid, reason="overdue", reject_reason="negative"
                    )
                logger.info(
                    "Overdue enforcement: %d lock(s) created, %d newly suspended "
                    "for account %s",
                    lock_count,
                    len(newly_suspended_ids),
                    account_id,
                )
        except Exception as exc:
            logger.warning(
                "Failed to auto-suspend account %s for overdue invoice: %s",
                account_id,
                exc,
            )
