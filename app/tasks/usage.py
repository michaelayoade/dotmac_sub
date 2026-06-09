import logging

from app.celery_app import celery_app
from app.schemas.usage import UsageRatingRunRequest
from app.services import usage as usage_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.usage.run_usage_rating")
def run_usage_rating():
    session = SessionLocal()
    try:
        usage_service.usage_rating_runs.run(session, UsageRatingRunRequest())
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.usage.import_radius_accounting")
def import_radius_accounting():
    session = SessionLocal()
    try:
        result = usage_service.import_radius_accounting(session)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.usage.evaluate_fup_rules")
def evaluate_fup_rules() -> dict[str, int]:
    """Evaluate FUP rules for all active subscriptions and apply enforcement.

    Runs periodically to check usage against FUP thresholds and apply
    throttle/block/notify actions. Also handles time-based profile switching
    (e.g., night boost) and FUP state resets at period boundaries.
    """
    from datetime import UTC, datetime

    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.fup import FupPolicy
    from app.models.fup_state import FupActionStatus
    from app.services.events import emit_event
    from app.services.events.types import EventType
    from app.services.fup import evaluate_rules
    from app.services.fup_state import fup_state

    session = SessionLocal()
    try:
        now = datetime.now(UTC)
        processed = 0
        enforced = 0
        reset = 0
        # Customer notifications to emit AFTER the enforcement commit, so a
        # notification failure can't roll back enforcement state. Each entry:
        # {subscriber_id, kind, rule_name, threshold_gb, used_gb}.
        pending_notifs: list[dict] = []

        # Find all active subscriptions that have FUP policies
        subscriptions = (
            session.query(Subscription)
            .join(FupPolicy, FupPolicy.offer_id == Subscription.offer_id)
            .filter(Subscription.status == SubscriptionStatus.active)
            .filter(FupPolicy.is_active.is_(True))
            .all()
        )

        for sub in subscriptions:
            processed += 1

            # Check if FUP state needs reset (period boundary crossed)
            state = fup_state.get(session, str(sub.id))
            if state and state.cap_resets_at and now >= state.cap_resets_at:
                fup_state.clear(session, str(sub.id))
                reset += 1
                logger.info("Reset FUP state for subscription %s", sub.id)
                continue

            # Get current usage from quota bucket
            from app.services.usage import _resolve_or_create_quota_bucket

            bucket = _resolve_or_create_quota_bucket(session, sub, now)
            if not bucket:
                continue

            current_usage = float(bucket.used_gb or 0)

            # Status persisted from a prior run — used to notify the customer
            # only on a *transition* into throttled/blocked (not every tick).
            prior_status = state.action_status.value if state else "none"

            # Evaluate rules
            results = evaluate_rules(
                session,
                str(sub.offer_id),
                current_usage_gb=current_usage,
                current_time=now,
            )

            triggered = [r for r in results if r.get("triggered")]
            if triggered:
                # Find the highest-severity triggered rule
                for rule_result in reversed(triggered):
                    if rule_result.get("action") == "block":
                        emit_event(
                            session,
                            EventType.usage_exhausted,
                            {
                                "subscription_id": str(sub.id),
                                "offer_id": str(sub.offer_id),
                                "rule_id": rule_result.get("rule_id"),
                                "current_usage_gb": current_usage,
                                "threshold_gb": rule_result.get("threshold_gb"),
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
                                }
                            )
                        break
                    elif rule_result.get("action") == "reduce_speed":
                        emit_event(
                            session,
                            EventType.usage_exhausted,
                            {
                                "subscription_id": str(sub.id),
                                "offer_id": str(sub.offer_id),
                                "rule_id": rule_result.get("rule_id"),
                                "current_usage_gb": current_usage,
                                "threshold_gb": rule_result.get("threshold_gb"),
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
                                }
                            )
                        break
            elif prior_status == "none":
                # Not yet enforced — warn once when usage crosses 80% of the
                # nearest threshold. Mark the state 'notified' so we don't repeat
                # it every tick; the period-boundary reset above clears it.
                ratios = [
                    (current_usage / r["threshold_gb"], r)
                    for r in results
                    if r.get("threshold_gb")
                ]
                if ratios:
                    ratio, r = max(ratios, key=lambda x: x[0])
                    if 0.8 <= ratio < 1.0:
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

        session.commit()
        # Notifications are sent after the enforcement commit so a delivery
        # failure never rolls back enforcement state.
        notified = _emit_fup_notifications(session, pending_notifs)
        logger.info(
            "FUP evaluation: %d processed, %d enforced, %d reset, %d notified",
            processed,
            enforced,
            reset,
            notified,
        )
        return {
            "processed": processed,
            "enforced": enforced,
            "reset": reset,
            "notified": notified,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _build_fup_notification(kind: str, rule_name, threshold_gb, used_gb):
    """(subject, body) for a customer FUP notification."""
    plan = rule_name or "your plan"
    if kind == "blocked":
        return (
            "Service paused",
            f"You've reached the fair-usage limit on {plan}. Service is paused "
            "until your next cycle — top up to restore it.",
        )
    if kind == "throttled":
        return (
            "Speed reduced",
            f"You've reached the fair-usage limit on {plan}. Your speed is "
            "reduced until your next cycle — top up to restore full speed.",
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


def _emit_fup_notifications(session, pending: list[dict]) -> int:
    """Create in-app/push notifications for queued FUP events. Best-effort:
    a failure on one notification never affects the others or enforcement."""
    if not pending:
        return 0
    from app.models.notification import NotificationChannel
    from app.models.subscriber import Subscriber
    from app.schemas.notification import NotificationCreate
    from app.services.notification import notifications as notifications_svc

    sent = 0
    for n in pending:
        try:
            subscriber = session.get(Subscriber, n["subscriber_id"])
            recipient = getattr(subscriber, "email", None)
            if not recipient:
                continue
            subject, body = _build_fup_notification(
                n["kind"], n.get("rule_name"), n.get("threshold_gb"), n.get("used_gb")
            )
            notifications_svc.create(
                session,
                NotificationCreate(
                    channel=NotificationChannel.push,
                    subscriber_id=n["subscriber_id"],
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    category="fup",
                    event_type=f"fup_{n['kind']}",
                ),
            )
            sent += 1
        except Exception:
            logger.warning("Failed to emit FUP notification", exc_info=True)
    return sent
