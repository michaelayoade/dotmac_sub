import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.schemas.usage import UsageRatingRunRequest
from app.services import usage as usage_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.usage.run_usage_rating")
def run_usage_rating():
    session = SessionLocal()
    try:
        usage_service.usage_rating_runs.run(session, UsageRatingRunRequest())
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.usage.import_radius_accounting")
def import_radius_accounting():
    session = SessionLocal()
    try:
        return usage_service.import_radius_accounting(session)
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

            bucket = _resolve_or_create_quota_bucket(session, sub)
            if not bucket:
                continue

            current_usage = float(bucket.used_gb or 0)

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
                        break

        session.commit()
        logger.info(
            "FUP evaluation: %d processed, %d enforced, %d reset",
            processed, enforced, reset,
        )
        return {"processed": processed, "enforced": enforced, "reset": reset}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
