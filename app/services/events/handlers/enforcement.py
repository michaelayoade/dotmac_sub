"""Event-driven enforcement for sessions and FUP actions."""

import logging

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.services import enforcement_event_policy
from app.services import radius as radius_service
from app.services import radius_reject as radius_reject_service
from app.services.enforcement import (
    _resolve_effective_profile,
    apply_radius_profile_to_account,
    disconnect_account_sessions,
    disconnect_subscription_sessions,
    remove_subscription_address_list_block,
    update_subscription_sessions,
)
from app.services.events.types import Event, EventType
from app.services.radius_access_state import (
    derive_access_state,
    set_subscription_access_state,
)

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.subscription_suspended,
        EventType.subscription_canceled,
        EventType.subscription_expired,
        EventType.subscription_activated,
        EventType.subscription_resumed,
        EventType.subscription_upgraded,
        EventType.subscription_downgraded,
        EventType.subscriber_throttled,
        EventType.usage_exhausted,
        EventType.payment_received,
        EventType.invoice_overdue,
    }
)


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
        elif event.event_type == EventType.subscriber_unthrottled:
            self._handle_account_unthrottle(db, event)
        elif event.event_type == EventType.usage_exhausted:
            self._handle_usage_exhausted(db, event)
        elif event.event_type == EventType.payment_received:
            self._handle_payment_received(db, event)
        elif event.event_type == EventType.invoice_overdue:
            self._handle_invoice_overdue(db, event)

    def _shadow_write_access_state(self, db: Session, subscription_id: str) -> None:
        """Mirror the derived access state locally, and to radusergroup when
        group routing is enabled.

        ``subscription.access_state`` is now an operational truth for portals
        and audits, so keep it current even while the external radusergroup
        path remains feature-flagged off.
        """
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return
        subscriber = (
            db.get(Subscriber, sub.subscriber_id) if sub.subscriber_id else None
        )
        from app.services.walled_garden_policy import resolve_subscription_restriction

        restriction = resolve_subscription_restriction(db, sub, account=subscriber)
        state = derive_access_state(
            sub.status,
            restriction_mode=(restriction.effective_mode if restriction else None),
        )
        target = state.value if state else None
        if getattr(sub, "access_state", None) != target:
            sub.access_state = target
            db.flush()

        if not enforcement_event_policy.group_routing_enabled(db):
            return
        try:
            result = set_subscription_access_state(db, str(subscription_id), state)
            logger.info(
                "shadow access_state: sub=%s state=%s %s",
                subscription_id,
                state.value if state else None,
                result,
            )
        except Exception as exc:
            logger.warning(
                "shadow access_state write failed for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _enqueue_subscription_session_cleanup(
        self, subscription_id: str, *, reason: str
    ) -> None:
        try:
            from app.tasks.enforcement import cleanup_subscription_block_sessions

            cleanup_subscription_block_sessions.delay(
                str(subscription_id), reason=reason
            )
        except Exception as exc:
            logger.error(
                "Failed to enqueue session cleanup for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _enforce_subscription_block(
        self,
        db: Session,
        subscription_id: str,
        *,
        reason: str = "suspended",
        reject_reason: str = "blocked",
        terminal: bool = False,
    ) -> None:
        """Apply RADIUS reject, block/remove credentials, disconnect sessions,
        and add address-list block for a single subscription. Callable both
        from the event-driven path and directly after ``emit=False`` lifecycle
        calls. Each step is individually guarded so one failure does not
        prevent the remaining enforcement actions.

        When ``terminal=True`` (subscription canceled), credentials are
        fully removed. Otherwise (suspended) they're flagged with a single
        ``Auth-Type := Reject`` row, so unblock is a single DELETE rather
        than a full credential rebuild."""
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

        # External radcheck/radreply state: radius_population is the
        # SOLE writer (single-writer decision, 2026-06-11). The previous
        # remove/block_external_radius_credentials calls here acted on the
        # WHOLE SUBSCRIBER — suspending one subscription wiped auth for the
        # subscriber's other active logins, and their writes fought the
        # populate sweeps. Instead, enqueue an immediate full refresh (~3s,
        # idempotent) so the status change reaches radcheck within seconds.
        if subscription:
            try:
                from app.tasks.radius_population import refresh_radius_from_subs

                refresh_radius_from_subs.delay()
            except Exception as exc:
                logger.error(
                    "Failed to enqueue RADIUS refresh for subscriber %s: %s "
                    "(periodic sweep will converge within 15 min)",
                    subscription.subscriber_id,
                    exc,
                )

        # Phase 3 shadow write — mirror the derived state to radusergroup.
        # No-op unless the enforcement event policy enables group routing.
        self._shadow_write_access_state(db, str(subscription_id))

        # Slow NAS cleanup runs out-of-band so the authoritative DB/RADIUS
        # reject state is not held hostage by session disconnect latency.
        self._enqueue_subscription_session_cleanup(str(subscription_id), reason=reason)

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
            terminal=(reason == "canceled"),
        )

    def _handle_subscription_cancel(self, db: Session, event: Event) -> None:
        self._handle_subscription_block(db, event, "canceled")

    def _handle_subscription_restore(self, db: Session, event: Event) -> None:
        from app.services.account_lifecycle import compute_account_status

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            return
        refresh_enabled = (
            enforcement_event_policy.refresh_sessions_on_profile_change_enabled(db)
        )

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

        # Lift the Auth-Type := Reject overlay before the reconcile rebuild,
        # so the rebuild doesn't carry the block forward via the
        # status-aware sync path.
        if subscription:
            try:
                radius_service.unblock_external_radius_credentials(
                    db, str(subscription.subscriber_id)
                )
            except Exception as exc:
                logger.error(
                    "Failed to unblock RADIUS credentials for subscriber %s: %s",
                    subscription.subscriber_id,
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

        # Phase 3 shadow write — mirror the restored state to radusergroup.
        # No-op unless the enforcement event policy enables group routing.
        self._shadow_write_access_state(db, str(subscription_id))

        # Converge radcheck/radreply to the restored state within seconds
        # via the single-writer sweep.
        try:
            from app.tasks.radius_population import refresh_radius_from_subs

            refresh_radius_from_subs.delay()
        except Exception as exc:
            logger.error(
                "Failed to enqueue RADIUS refresh on restore for %s: %s",
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
        # Rebuild radreply so the credential's throttle profile actually shapes
        # the line — populate() now honours the credential override, but only on
        # a sweep. Without this immediate refresh the throttle wouldn't land
        # until the next scheduled sweep (up to ~15 min). Best-effort; the
        # periodic sweep is the backstop if the enqueue is lost.
        try:
            from app.tasks.radius_population import refresh_radius_from_subs

            refresh_radius_from_subs.delay()
        except Exception:
            logger.warning(
                "Failed to enqueue radius refresh after throttle for account %s",
                account_id,
                exc_info=True,
            )
        refresh_enabled = (
            enforcement_event_policy.refresh_sessions_on_profile_change_enabled(db)
        )
        try:
            if refresh_enabled:
                disconnect_account_sessions(db, str(account_id), reason="throttle")
        except Exception as exc:
            logger.error(
                "Failed to disconnect sessions for account %s: %s",
                account_id,
                exc,
            )

    def _handle_account_unthrottle(self, db: Session, event: Event) -> None:
        """Push the restored profile to RADIUS as promptly as the throttle landed.

        The throttle enqueued a refresh and the release did not, so a customer who
        paid stayed rate-limited until the next scheduled sweep. Mirror it.

        Sessions are refreshed so the new (faster) profile applies to the live
        session rather than only to the next reconnect.
        """
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            logger.debug("Skipping unthrottle enforcement: event missing account_id")
            return
        try:
            from app.tasks.radius_population import refresh_radius_from_subs

            refresh_radius_from_subs.delay()
        except Exception:
            logger.warning(
                "Failed to enqueue radius refresh after unthrottle for account %s",
                account_id,
                exc_info=True,
            )
        refresh_enabled = (
            enforcement_event_policy.refresh_sessions_on_profile_change_enabled(db)
        )
        try:
            if refresh_enabled:
                disconnect_account_sessions(db, str(account_id), reason="unthrottle")
        except Exception as exc:
            logger.error(
                "Failed to refresh sessions after unthrottle for account %s: %s",
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
        action = enforcement_event_policy.fup_action(db, event.payload.get("action"))
        if action == "none":
            return

        # Resolve offer_id and rule_id from payload for state tracking
        offer_id = event.payload.get("offer_id")
        rule_id = event.payload.get("rule_id")
        cap_resets_at_raw = event.payload.get("cap_resets_at")
        # Set when a FUP "block" is downgraded to a full suspend (no captive
        # redirect), so the persisted state distinguishes it from a non-payment
        # suspension.
        fup_block_downgraded = False

        fup_access_mode = None
        if action == "block":
            from app.models.enforcement_lock import AccessRestrictionMode
            from app.models.subscriber import Subscriber
            from app.services.walled_garden_policy import (
                resolve_walled_garden_decision,
            )

            subscriber = db.get(Subscriber, account_id)
            if subscriber is None:
                return
            decision = resolve_walled_garden_decision(
                db,
                subscriber,
                requested_mode=AccessRestrictionMode.captive,
            )
            fup_access_mode = decision.effective_mode
            action = "suspend"
            fup_block_downgraded = fup_access_mode == AccessRestrictionMode.hard_reject
            if fup_block_downgraded:
                logger.warning(
                    "fup_block_downgraded_to_suspend subscription=%s account=%s "
                    "rule=%s reason=%s",
                    subscription_id,
                    account_id,
                    rule_id,
                    decision.reason,
                )
        if action == "suspend":
            from app.models.enforcement_lock import (
                AccessRestrictionMode,
                EnforcementReason,
            )
            from app.services.account_lifecycle import suspend_subscription

            fup_source = f"fup_rule:{rule_id}" if rule_id else "fup_exhausted"
            try:
                suspend_subscription(
                    db,
                    str(subscription_id),
                    reason=EnforcementReason.fup,
                    source=fup_source,
                    access_mode=fup_access_mode or AccessRestrictionMode.hard_reject,
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
                    notes=(
                        "FUP cap: block downgraded to suspend (captive not enabled)"
                        if fup_block_downgraded
                        else "FUP suspension applied"
                    ),
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
        throttle_profile_id = enforcement_event_policy.fup_throttle_radius_profile_id(
            db
        )
        if not throttle_profile_id:
            logger.warning(
                "FUP throttle profile not configured. "
                "Set 'fup_throttle_radius_profile_id' in usage domain settings."
            )
            return
        # Capture the subscriber's current full-speed profile BEFORE the
        # throttle overwrites it, so the period-reset lift can restore it. The
        # offer's effective profile is the durable "should be" value.
        original_profile_id = None
        _sub_for_profile = db.get(Subscription, subscription_id)
        if _sub_for_profile is not None:
            _orig = _resolve_effective_profile(db, _sub_for_profile)
            original_profile_id = str(_orig.id) if _orig else None
        try:
            updated = apply_radius_profile_to_account(
                db, str(account_id), str(throttle_profile_id)
            )
            if updated:
                refresh_enabled = (
                    enforcement_event_policy.refresh_sessions_on_profile_change_enabled(
                        db
                    )
                )
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
                    original_profile_id=original_profile_id,
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
        original_profile_id: str | None = None,
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
                original_profile_id=original_profile_id,
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
        """Submit payment observation to the financial-access reconciler."""
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
            from app.services.account_lifecycle import compute_account_status

            compute_account_status(db, str(account_id))
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
        """Treat invoice-overdue as observation; dunning owns every action."""
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            return
        logger.info(
            "Invoice overdue observed for account %s; dunning policy owns "
            "notification, throttle, suspension, and rejection",
            account_id,
        )
