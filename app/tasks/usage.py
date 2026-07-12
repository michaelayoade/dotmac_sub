import logging

from app.celery_app import celery_app
from app.schemas.usage import UsageRatingRunRequest
from app.services import usage as usage_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.usage.run_usage_rating")
def run_usage_rating():
    with db_session_adapter.session() as session:
        usage_service.usage_rating_runs.run(session, UsageRatingRunRequest())


@celery_app.task(name="app.tasks.usage.import_radius_accounting")
def import_radius_accounting():
    from app.tasks._postgres_lock import postgres_session_advisory_lock

    with postgres_session_advisory_lock(_RADIUS_ACCOUNTING_IMPORT_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("RADIUS accounting import skipped: another run is active")
            return {
                "ok": True,
                "processed": 0,
                "created_or_updated": 0,
                "cursor": None,
                "skipped_locked": 1,
            }
        return _import_radius_accounting_locked()


def _import_radius_accounting_locked():
    with db_session_adapter.session() as session:
        result = usage_service.import_radius_accounting(session)
        return result


@celery_app.task(name="app.tasks.usage.reap_stale_radius_sessions")
def reap_stale_radius_sessions():
    """Close accounting sessions whose feed went silent (NAS reboot / lost
    Stop packet) so they stop rendering as "active" forever. Safe only
    because the importer's refresh pass keeps last_update_at fresh for
    genuinely live sessions."""
    with db_session_adapter.session() as session:
        result = usage_service.reap_stale_radius_sessions(session)
        return result


@celery_app.task(name="app.tasks.usage.notify_expiring_data_bundles")
def notify_expiring_data_bundles():
    """Warn customers whose data bundles lapse within the next 24 hours.

    Runs daily, so each bundle falls inside the [now, now+24h) window exactly
    once — natural dedupe without extra state. Emits usage.addon_expiring,
    which the notification handler fans out to push + email."""
    from datetime import UTC, datetime, timedelta

    from app.models.catalog import AddOn, SubscriptionAddOn
    from app.services.events import emit_event
    from app.services.events.types import EventType

    with db_session_adapter.session() as session:
        now = datetime.now(UTC)
        window_end = now + timedelta(hours=24)
        rows = (
            session.query(SubscriptionAddOn, AddOn)
            .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
            .filter(AddOn.grant_gb.isnot(None))
            .filter(SubscriptionAddOn.end_at.isnot(None))
            .filter(SubscriptionAddOn.end_at >= now)
            .filter(SubscriptionAddOn.end_at < window_end)
            .all()
        )
        notified = 0
        for sub_add_on, add_on in rows:
            subscription = sub_add_on.subscription
            if subscription is None:
                continue
            grant = (add_on.grant_gb or 0) * int(sub_add_on.quantity or 1)
            emit_event(
                session,
                EventType.addon_expiring,
                {
                    "subscription_id": str(sub_add_on.subscription_id),
                    "account_id": str(subscription.subscriber_id),
                    "addon_name": add_on.name,
                    "grant_gb": str(grant),
                    "expires_at": sub_add_on.end_at.isoformat(),
                },
                subscription_id=sub_add_on.subscription_id,
                account_id=subscription.subscriber_id,
            )
            notified += 1
        return {"notified": notified}


@celery_app.task(name="app.tasks.usage.meter_usage_into_quota")
def meter_usage_into_quota():
    """Roll imported RADIUS accounting into the current period's quota buckets
    for capped subscriptions (the metering that feeds FUP/overage)."""
    with db_session_adapter.session() as session:
        result = usage_service.meter_usage_into_quota(session)
    changed_subscription_ids = result.get("changed_subscription_ids") or []
    if changed_subscription_ids:
        evaluate_fup_rules.apply_async(
            kwargs={
                "subscription_ids": changed_subscription_ids,
                "source": "usage_metering",
            },
            queue="billing",
        )
    return result


def _fup_should_enforce(
    *,
    prior_status: str,
    target_status: str,
    cooldown_minutes: int,
    state,
    now,
) -> bool:
    """Transition-driven FUP enforcement to stop per-tick RADIUS churn.

    Enforce when *entering* the throttled/blocked state; once already in it,
    re-assert only after ``cooldown_minutes`` has elapsed (0 = never re-assert).
    Previously the task re-emitted ``usage_exhausted`` and re-applied the RADIUS
    profile / SSH address-list on every single tick.
    """
    from datetime import UTC

    if prior_status != target_status:
        return True
    if cooldown_minutes and state is not None and state.last_evaluated_at is not None:
        last = state.last_evaluated_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (now - last).total_seconds() / 60 >= cooldown_minutes
    return False


_FUP_EVALUATION_LOCK_KEY = 778_003
_RADIUS_ACCOUNTING_IMPORT_LOCK_KEY = 778_004


@celery_app.task(name="app.tasks.usage.evaluate_fup_rules")
def evaluate_fup_rules(
    subscription_ids: list[str] | None = None,
    source: str = "scheduled_full_sweep",
) -> dict[str, int]:
    from sqlalchemy import func, select

    lock_db = SessionLocal()
    try:
        bind = lock_db.bind
        is_pg = bind is not None and bind.dialect.name == "postgresql"
        if is_pg:
            acquired = lock_db.execute(
                select(func.pg_try_advisory_lock(_FUP_EVALUATION_LOCK_KEY))
            ).scalar()
            # Commit immediately after taking the session-level advisory lock.
            # The lock survives commit, and the connection is no longer left
            # "idle in transaction" while the FUP sweep runs.
            lock_db.commit()
            if not acquired:
                logger.info("FUP evaluation skipped: another run is still active")
                return {
                    "processed": 0,
                    "enforced": 0,
                    "submonthly_no_data": 0,
                    "reset": 0,
                    "notifications": 0,
                    "skipped_locked": 1,
                }
        try:
            return _evaluate_fup_rules_locked(
                subscription_ids=subscription_ids,
                source=source,
            )
        finally:
            if is_pg:
                lock_db.execute(
                    select(func.pg_advisory_unlock(_FUP_EVALUATION_LOCK_KEY))
                )
                lock_db.commit()
    finally:
        lock_db.close()


def _evaluate_fup_rules_locked(
    *,
    subscription_ids: list[str] | None = None,
    source: str = "scheduled_full_sweep",
) -> dict[str, int]:
    """Evaluate FUP rules for all active subscriptions and apply enforcement.

    Runs periodically to check usage against FUP thresholds and apply
    throttle/block/notify actions. Also handles time-based profile switching
    (e.g., night boost) and FUP state resets at period boundaries.
    """
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import or_

    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.domain_settings import SettingDomain
    from app.models.fup import FupPolicy
    from app.models.fup_state import FupActionStatus, FupState
    from app.services import control_registry, settings_spec
    from app.services.events import emit_event
    from app.services.events.types import EventType
    from app.services.fup import evaluate_rules
    from app.services.fup_state import fup_state
    from app.services.usage import _parse_warning_thresholds

    session = SessionLocal()
    try:
        now = datetime.now(UTC)
        processed = 0
        enforced = 0
        submonthly_no_data = 0
        throttle_unconfigured = 0
        reset = 0
        # Customer notifications to emit AFTER the enforcement commit, so a
        # notification failure can't roll back enforcement state. Each entry:
        # {subscriber_id, kind, rule_name, threshold_gb, used_gb}.
        pending_notifs: list[dict] = []

        # "Approaching" warnings reuse the configurable usage-warning settings
        # (usage_warning_enabled / usage_warning_thresholds, e.g. "0.8,0.9") so
        # the percentage is operator-controlled, not hardcoded. We warn at the
        # lowest configured threshold.
        warn_enabled = control_registry.is_enabled(session, "usage.warnings")
        _thr_raw = settings_spec.resolve_value(
            session, SettingDomain.usage, "usage_warning_thresholds"
        )
        _parsed = _parse_warning_thresholds(
            str(_thr_raw) if _thr_raw is not None else None
        )
        warn_ratio = float(_parsed[0]) if _parsed else 0.8

        # When the FUP "reduce_speed" action has no throttle RADIUS profile
        # configured, the enforcement handler can't actually throttle. Read the
        # profile once so the loop can skip that notification and surface the
        # misconfiguration instead.
        throttle_profile_configured = bool(
            settings_spec.resolve_value(
                session, SettingDomain.usage, "fup_throttle_radius_profile_id"
            )
        )

        enforced_states = (
            FupActionStatus.notified,
            FupActionStatus.throttled,
            FupActionStatus.blocked,
        )
        subscription_uuid_filter = None
        if subscription_ids is not None:
            subscription_uuid_filter = []
            for raw_id in subscription_ids:
                try:
                    subscription_uuid_filter.append(uuid.UUID(str(raw_id)))
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping invalid FUP subscription id %r from %s",
                        raw_id,
                        source,
                    )

        # Find active subscriptions with FUP policies, plus subscriptions
        # already under FUP control. The latter matters for cap-boundary
        # auto-lift: a blocked subscription is suspended, so an active-only
        # scan would never clear it after the reset window.
        subscriptions_query = (
            session.query(Subscription)
            .join(FupPolicy, FupPolicy.offer_id == Subscription.offer_id)
            .outerjoin(FupState, FupState.subscription_id == Subscription.id)
            .filter(
                or_(
                    Subscription.status == SubscriptionStatus.active,
                    FupState.action_status.in_(enforced_states),
                )
            )
            .filter(FupPolicy.is_active.is_(True))
        )
        if subscription_uuid_filter is not None:
            if not subscription_uuid_filter:
                return {
                    "processed": 0,
                    "enforced": 0,
                    "reset": 0,
                    "notified": 0,
                    "submonthly_no_data": 0,
                    "throttle_unconfigured": 0,
                    "targeted": 1,
                }
            subscriptions_query = subscriptions_query.filter(
                Subscription.id.in_(subscription_uuid_filter)
            )
        subscriptions = subscriptions_query.all()

        for sub in subscriptions:
            processed += 1

            # Check if FUP state needs reset (period boundary crossed)
            state = fup_state.get(session, str(sub.id))
            if state and state.cap_resets_at and now >= state.cap_resets_at:
                # Lift the actual enforcement (RADIUS profile / address-list
                # block / suspension), not just the state row — otherwise the
                # subscriber stays throttled/blocked forever past the reset.
                from app.services.enforcement import lift_fup_enforcement

                lift_fup_enforcement(session, str(sub.id))
                reset += 1
                logger.info("Lifted FUP enforcement for subscription %s", sub.id)
                session.commit()
                continue

            # Get current usage from quota bucket
            from app.services.usage import _resolve_or_create_quota_bucket

            bucket = _resolve_or_create_quota_bucket(session, sub, now)
            if not bucket:
                session.commit()
                continue

            current_usage = float(bucket.used_gb or 0)

            # Per-period usage so daily/weekly rules measure their own window.
            # A monthly-only offer yields {"monthly": current_usage} — identical
            # to the legacy path (no extra queries, no async bridge).
            from app.services.fup_usage import build_usage_by_period

            usage_by_period = build_usage_by_period(
                session, sub, str(sub.offer_id), now, current_usage
            )

            # Status persisted from a prior run — used to notify the customer
            # only on a *transition* into throttled/blocked (not every tick).
            prior_status = state.action_status.value if state else "none"

            # Evaluate rules
            results = evaluate_rules(
                session,
                str(sub.offer_id),
                current_usage_gb=current_usage,
                current_time=now,
                usage_by_period=usage_by_period,
            )

            # Safeguard tripwire (#21): a sub-monthly rule whose window had no
            # usage data (metrics store down / no samples) reads 0 and silently
            # under-enforces — a real over-user would NOT be throttled. Surface
            # it so the gap is visible rather than invisible.
            for r in results:
                if r.get("usage_source") == "no_data":
                    submonthly_no_data += 1
                    logger.warning(
                        "FUP %s window for sub %s rule %s had no usage data — "
                        "not enforced this run (metrics store down or no samples)",
                        r.get("consumption_period"),
                        sub.id,
                        r.get("rule_id"),
                    )

            triggered = [r for r in results if r.get("triggered")]

            if triggered:
                # Find the highest-severity triggered rule
                for rule_result in reversed(triggered):
                    # Cap auto-lifts at the end of THIS rule's consumption window
                    # (daily -> next local midnight, weekly -> next Monday,
                    # monthly -> the billing-period end). Falls back to the quota
                    # period boundary when no window is attached.
                    cap_resets_at = rule_result.get("window_end") or (
                        bucket.period_end.isoformat() if bucket.period_end else None
                    )

                    # Defense-in-depth (#21): never enforce a sub-monthly rule on
                    # a blind reading (already reads 0, but make the intent
                    # explicit and cover a 0-GB threshold).
                    if rule_result.get("usage_source") == "no_data":
                        continue

                    # Observability: structured record of every sub-monthly
                    # enforcement decision for ops review before/after rollout.
                    if rule_result.get("consumption_period") != "monthly":
                        logger.info(
                            "fup_submonthly_enforce sub=%s rule=%s period=%s "
                            "used_gb=%s threshold_gb=%s source=%s authoritative=%s "
                            "window=%s..%s action=%s",
                            sub.id,
                            rule_result.get("rule_id"),
                            rule_result.get("consumption_period"),
                            rule_result.get("current_usage_gb"),
                            rule_result.get("threshold_gb"),
                            rule_result.get("usage_source"),
                            rule_result.get("is_authoritative"),
                            rule_result.get("window_start"),
                            rule_result.get("window_end"),
                            rule_result.get("action"),
                        )

                    if rule_result.get("action") == "block":
                        if _fup_should_enforce(
                            prior_status=prior_status,
                            target_status="blocked",
                            cooldown_minutes=rule_result.get("cooldown_minutes") or 0,
                            state=state,
                            now=now,
                        ):
                            emit_event(
                                session,
                                EventType.usage_exhausted,
                                {
                                    "subscription_id": str(sub.id),
                                    "offer_id": str(sub.offer_id),
                                    "rule_id": rule_result.get("rule_id"),
                                    "action": rule_result.get("action"),
                                    "current_usage_gb": current_usage,
                                    "threshold_gb": rule_result.get("threshold_gb"),
                                    "cap_resets_at": cap_resets_at,
                                },
                                subscription_id=sub.id,
                                account_id=sub.subscriber_id,
                            )
                            enforced += 1
                        if prior_status != "blocked":
                            pending_notifs.append(
                                {
                                    "subscriber_id": sub.subscriber_id,
                                    "kind": "blocked",
                                    "rule_name": rule_result.get("name"),
                                    "threshold_gb": rule_result.get("threshold_gb"),
                                    "used_gb": current_usage,
                                    "cap_resets_at": cap_resets_at,
                                }
                            )
                        _maybe_queue_repeat_upsell(
                            session, sub, bucket, rule_result, pending_notifs
                        )
                        break
                    elif rule_result.get("action") == "reduce_speed":
                        # No throttle profile → the handler can't actually reduce
                        # speed. Skip the no-op enforcement AND the customer
                        # notification (don't claim a throttle that didn't happen);
                        # count it so the misconfiguration is visible to ops.
                        if not throttle_profile_configured:
                            throttle_unconfigured += 1
                            logger.warning(
                                "FUP reduce_speed triggered for sub %s rule %s but "
                                "no throttle profile configured "
                                "(usage.fup_throttle_radius_profile_id) — not "
                                "enforced and customer NOT notified",
                                sub.id,
                                rule_result.get("rule_id"),
                            )
                            break
                        if _fup_should_enforce(
                            prior_status=prior_status,
                            target_status="throttled",
                            cooldown_minutes=rule_result.get("cooldown_minutes") or 0,
                            state=state,
                            now=now,
                        ):
                            emit_event(
                                session,
                                EventType.usage_exhausted,
                                {
                                    "subscription_id": str(sub.id),
                                    "offer_id": str(sub.offer_id),
                                    "rule_id": rule_result.get("rule_id"),
                                    "action": rule_result.get("action"),
                                    "current_usage_gb": current_usage,
                                    "threshold_gb": rule_result.get("threshold_gb"),
                                    "cap_resets_at": cap_resets_at,
                                },
                                subscription_id=sub.id,
                                account_id=sub.subscriber_id,
                            )
                            enforced += 1
                        if prior_status != "throttled":
                            pending_notifs.append(
                                {
                                    "subscriber_id": sub.subscriber_id,
                                    "kind": "throttled",
                                    "rule_name": rule_result.get("name"),
                                    "threshold_gb": rule_result.get("threshold_gb"),
                                    "used_gb": current_usage,
                                    "cap_resets_at": cap_resets_at,
                                }
                            )
                        _maybe_queue_repeat_upsell(
                            session, sub, bucket, rule_result, pending_notifs
                        )
                        break
            elif prior_status == "none" and warn_enabled:
                # Not yet enforced — warn once when usage crosses the configured
                # warning ratio of the nearest threshold. Mark the state
                # 'notified' so we don't repeat it every tick; the
                # period-boundary reset above clears it.
                ratios = [
                    (current_usage / r["threshold_gb"], r)
                    for r in results
                    if r.get("threshold_gb")
                ]
                if ratios:
                    ratio, r = max(ratios, key=lambda x: x[0])
                    if warn_ratio <= ratio < 1.0:
                        fup_state.apply_action(
                            session,
                            str(sub.id),
                            offer_id=str(sub.offer_id),
                            rule_id=r.get("rule_id"),
                            action_status=FupActionStatus.notified,
                            notes="approaching fup limit",
                        )
                        pending_notifs.append(
                            {
                                "subscriber_id": sub.subscriber_id,
                                "kind": "approaching",
                                "rule_name": r.get("name"),
                                "threshold_gb": r.get("threshold_gb"),
                                "used_gb": current_usage,
                            }
                        )

            # Commit each subscription on its own so this periodic sweep never
            # holds a single transaction open across the whole subscription list.
            # Previously the one commit below ran only after the entire loop, so a
            # large active-subscriber set produced multi-minute "idle in
            # transaction" connections — pinning a pool slot and blocking
            # autovacuum. Per-subscription commits also make a mid-sweep failure
            # preserve already-enforced subscriptions (the sweep is idempotent).
            session.commit()

        session.commit()
        # Notifications are sent after the enforcement commit so a delivery
        # failure never rolls back enforcement state.
        notified = _emit_fup_notifications(session, pending_notifs)
        logger.info(
            "FUP evaluation (%s): %d processed, %d enforced, %d reset, "
            "%d notified, %d sub-monthly no-data, %d throttle-unconfigured",
            source,
            processed,
            enforced,
            reset,
            notified,
            submonthly_no_data,
            throttle_unconfigured,
        )
        return {
            "processed": processed,
            "enforced": enforced,
            "reset": reset,
            "notified": notified,
            "submonthly_no_data": submonthly_no_data,
            "throttle_unconfigured": throttle_unconfigured,
            "targeted": int(subscription_ids is not None),
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.usage.lift_expired_fup_enforcement")
def lift_expired_fup_enforcement() -> dict[str, int]:
    """Queue-independent safety-net that lifts FUP enforcement past its reset.

    The primary reset is inline in ``evaluate_fup_rules`` (billing queue); if
    that queue stalls, throttled/blocked customers would never be auto-lifted
    after their consumption window ends. This standalone sweep reads
    ``list_pending_reset`` and lifts each state independently, so reset survives
    a billing-queue outage. Idempotent — an already-cleared state is a no-op.
    """
    from datetime import UTC, datetime

    from app.services.enforcement import lift_fup_enforcement
    from app.services.fup_state import fup_state

    session = SessionLocal()
    lifted = 0
    errors = 0
    try:
        now = datetime.now(UTC)
        pending = fup_state.list_pending_reset(session, now)
        for state in pending:
            try:
                result = lift_fup_enforcement(session, str(state.subscription_id))
                if result.get("lifted"):
                    lifted += 1
                session.commit()
            except Exception as exc:
                session.rollback()
                errors += 1
                logger.error(
                    "Failed safety-net FUP lift for subscription %s: %s",
                    state.subscription_id,
                    exc,
                )
        logger.info(
            "FUP reset safety-net: %d pending, %d lifted, %d errors",
            len(pending),
            lifted,
            errors,
        )
        return {"pending": len(pending), "lifted": lifted, "errors": errors}
    finally:
        session.close()


def _hit_fup_in_window(session, subscriber_id, start, end) -> bool:
    """Did this subscriber get a throttled/blocked notification in [start, end)?
    Enforcement notifications fire once per transition, so one per cycle —
    making them a usable cross-cycle FUP-hit history without a new table."""
    from app.models.notification import Notification

    return (
        session.query(Notification.id)
        .filter(Notification.subscriber_id == subscriber_id)
        .filter(Notification.event_type.in_(["fup_throttled", "fup_blocked"]))
        .filter(Notification.created_at >= start)
        .filter(Notification.created_at < end)
        .first()
        is not None
    )


def _maybe_queue_repeat_upsell(session, sub, bucket, rule_result, pending_notifs):
    """Habitual-maxing nudge: enforced this cycle AND in >=1 of the previous
    two cycles (>=2 of the last 3) -> suggest a bigger plan, once per cycle.

    Best-effort: a failure here must never affect enforcement itself."""
    try:
        from app.models.notification import Notification

        if bucket is None or not bucket.period_start or not bucket.period_end:
            return
        period_len = bucket.period_end - bucket.period_start
        if period_len.total_seconds() <= 0:
            return

        # Once per cycle.
        already = (
            session.query(Notification.id)
            .filter(Notification.subscriber_id == sub.subscriber_id)
            .filter(Notification.event_type == "fup_repeat_upsell")
            .filter(Notification.created_at >= bucket.period_start)
            .first()
        )
        if already is not None:
            return

        prior_hits = sum(
            1
            for k in (1, 2)
            if _hit_fup_in_window(
                session,
                sub.subscriber_id,
                bucket.period_start - period_len * k,
                bucket.period_start - period_len * (k - 1),
            )
        )
        if prior_hits < 1:
            return

        pending_notifs.append(
            {
                "subscriber_id": sub.subscriber_id,
                "kind": "repeat_upsell",
                "rule_name": rule_result.get("name"),
                "threshold_gb": rule_result.get("threshold_gb"),
                "used_gb": None,
                "cycles": prior_hits + 1,
            }
        )
    except Exception:
        logger.warning("repeat-upsell check failed", exc_info=True)


def _fup_reset_phrase(cap_resets_at) -> str:
    """' on <date>' when a reset time is known, else ''. The FUP allowance resets
    at its window boundary (currently the calendar-month/quota-period end), which
    is NOT necessarily the subscriber's billing 'cycle' anchor — so the copy must
    not claim 'your next cycle'."""
    if not cap_resets_at:
        return ""
    from datetime import datetime

    try:
        if isinstance(cap_resets_at, str):
            value = datetime.fromisoformat(cap_resets_at)
        else:
            value = cap_resets_at
        return f" on {value.date().isoformat()}"
    except (ValueError, TypeError, AttributeError):
        return ""


def _build_fup_notification(
    kind: str, rule_name, threshold_gb, used_gb, cap_resets_at=None
):
    """(subject, body) for a customer FUP notification."""
    plan = rule_name or "your plan"
    when = _fup_reset_phrase(cap_resets_at)
    if kind == "blocked":
        return (
            "Service paused",
            f"You've reached the fair-usage limit on {plan}. Service is paused "
            f"until your data allowance resets{when} — or top up data to restore "
            "it now.",
        )
    if kind == "throttled":
        return (
            "Speed reduced",
            f"You've reached the fair-usage limit on {plan}. Your speed is "
            f"reduced until your data allowance resets{when} — or top up data to "
            "restore full speed now.",
        )
    if kind == "repeat_upsell":
        return (
            "Hitting your limit every month?",
            f"You've reached the fair-usage limit on {plan} several months "
            "in a row. A bigger plan gives you more full-speed data every "
            "cycle — see your upgrade options in the app.",
        )
    # approaching
    try:
        pct = int(round((used_gb / threshold_gb) * 100)) if threshold_gb else 80
    except (TypeError, ZeroDivisionError):
        pct = 80
    return (
        "Approaching your data limit",
        f"You've used about {pct}% of the fair-usage allowance on {plan}.",
    )


# Default channels per event kind. The shared notification channel policy can
# override these by event code (fup_blocked) or category (fup).
_FUP_NOTIFICATION_DEFAULT_CHANNELS = {
    "approaching": ("push",),
    "throttled": ("push", "email"),
    "blocked": ("push", "email"),
    "repeat_upsell": ("push", "email"),
}


def _emit_fup_notifications(session, pending: list[dict]) -> int:
    """Create customer notifications for queued FUP events, on the channels that
    fit each event (push always; email for enforcement). Best-effort: a failure
    on one notification or channel never affects the others or enforcement.
    Returns the number of (event) notifications for which at least one channel
    was created."""
    if not pending:
        return 0
    from app.models.subscriber import Subscriber
    from app.services.notification import notifications as notifications_svc

    sent = 0
    for n in pending:
        try:
            subscriber = session.get(Subscriber, n["subscriber_id"])
            subject, body = _build_fup_notification(
                n["kind"],
                n.get("rule_name"),
                n.get("threshold_gb"),
                n.get("used_gb"),
                n.get("cap_resets_at"),
            )
            event_type = f"fup_{n['kind']}"
            created = notifications_svc.queue_customer_notifications_for_policy(
                session,
                subscriber=subscriber,
                template_code=event_type,
                event_type=event_type,
                category="fup",
                default_channels=_FUP_NOTIFICATION_DEFAULT_CHANNELS.get(
                    n["kind"],
                    ("push",),
                ),
                subject=subject,
                body=body,
            )
            if created:
                sent += 1
        except Exception:
            logger.warning("Failed to emit FUP notification", exc_info=True)
    return sent
