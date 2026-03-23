"""Fair Usage Policy service for traffic-based speed reduction policies."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.fup import (
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
    FupPolicy,
    FupRule,
)
from app.services.common import coerce_uuid, validate_enum

logger = logging.getLogger(__name__)


class FupPolicies:
    """Manager for Fair Usage Policy configuration and rules."""

    @staticmethod
    def get_by_offer(db: Session, offer_id: str) -> FupPolicy | None:
        """Get the FUP policy for a catalog offer, or None if none exists.

        Args:
            db: Database session.
            offer_id: The catalog offer UUID.

        Returns:
            The FupPolicy or None.
        """
        stmt = (
            select(FupPolicy)
            .options(joinedload(FupPolicy.rules))
            .where(FupPolicy.offer_id == coerce_uuid(offer_id))
        )
        return db.scalars(stmt).unique().first()

    @staticmethod
    def get_or_create(db: Session, offer_id: str) -> FupPolicy:
        """Get existing FUP policy for an offer, or create an empty one.

        Args:
            db: Database session.
            offer_id: The catalog offer UUID.

        Returns:
            The existing or newly created FupPolicy.
        """
        uid = coerce_uuid(offer_id)
        stmt = (
            select(FupPolicy)
            .options(joinedload(FupPolicy.rules))
            .where(FupPolicy.offer_id == uid)
        )
        policy = db.scalars(stmt).unique().first()
        if policy:
            return policy

        policy = FupPolicy(offer_id=uid)
        db.add(policy)
        db.commit()
        db.refresh(policy)
        logger.info("Created FUP policy %s for offer %s", policy.id, offer_id)
        return policy

    @staticmethod
    def _get_policy(db: Session, policy_id: str) -> FupPolicy:
        """Fetch a policy by ID or raise 404."""
        policy = db.get(FupPolicy, coerce_uuid(policy_id))
        if not policy:
            raise HTTPException(status_code=404, detail="FUP policy not found")
        return policy

    @staticmethod
    def update_policy(db: Session, policy_id: str, **kwargs: Any) -> FupPolicy:
        """Update policy-level settings.

        Args:
            db: Database session.
            policy_id: The FUP policy UUID.
            **kwargs: Fields to update on the policy.

        Returns:
            The updated FupPolicy.
        """
        policy = FupPolicies._get_policy(db, policy_id)
        allowed_fields = {
            "traffic_accounting_start",
            "traffic_accounting_end",
            "traffic_inverse_interval",
            "online_accounting_start",
            "online_accounting_end",
            "online_inverse_interval",
            "traffic_days_of_week",
            "online_days_of_week",
            "is_active",
            "notes",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(policy, key, value)
        policy.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(policy)
        logger.info("Updated FUP policy %s", policy_id)
        return policy

    @staticmethod
    def add_rule(
        db: Session,
        policy_id: str,
        *,
        name: str,
        consumption_period: str,
        direction: str,
        threshold_amount: float,
        threshold_unit: str,
        action: str,
        speed_reduction_percent: float | None = None,
        sort_order: int | None = None,
        time_start: time | None = None,
        time_end: time | None = None,
        enabled_by_rule_id: str | None = None,
        days_of_week: list[int] | None = None,
        is_active: bool = True,
    ) -> FupRule:
        """Add a rule to an FUP policy.

        Args:
            db: Database session.
            policy_id: The FUP policy UUID.
            name: Human-readable rule name.
            consumption_period: One of monthly/daily/weekly.
            direction: One of up/down/up_down.
            threshold_amount: Data consumption threshold value.
            threshold_unit: One of mb/gb/tb.
            action: One of reduce_speed/block/notify.
            speed_reduction_percent: Percentage to reduce speed to
                (for reduce_speed action).

        Returns:
            The newly created FupRule.
        """
        policy = FupPolicies._get_policy(db, policy_id)

        # Determine next sort_order
        max_order_stmt = (
            select(FupRule.sort_order)
            .where(FupRule.policy_id == policy.id)
            .order_by(FupRule.sort_order.desc())
            .limit(1)
        )
        max_order = db.scalars(max_order_stmt).first()
        next_order = (max_order or 0) + 1

        rule = FupRule(
            policy_id=policy.id,
            name=name.strip(),
            sort_order=sort_order if sort_order is not None else next_order,
            consumption_period=validate_enum(
                consumption_period, FupConsumptionPeriod, "consumption_period"
            ),
            direction=validate_enum(direction, FupDirection, "direction"),
            threshold_amount=threshold_amount,
            threshold_unit=validate_enum(threshold_unit, FupDataUnit, "threshold_unit"),
            action=validate_enum(action, FupAction, "action"),
            speed_reduction_percent=speed_reduction_percent,
            time_start=time_start,
            time_end=time_end,
            enabled_by_rule_id=coerce_uuid(enabled_by_rule_id)
            if enabled_by_rule_id
            else None,
            days_of_week=days_of_week,
            is_active=is_active,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        logger.info("Added FUP rule %s to policy %s", rule.id, policy_id)
        return rule

    @staticmethod
    def _get_rule(db: Session, rule_id: str) -> FupRule:
        """Fetch a rule by ID or raise 404."""
        rule = db.get(FupRule, coerce_uuid(rule_id))
        if not rule:
            raise HTTPException(status_code=404, detail="FUP rule not found")
        return rule

    @staticmethod
    def update_rule(db: Session, rule_id: str, **kwargs: Any) -> FupRule:
        """Update fields on an existing FUP rule.

        Args:
            db: Database session.
            rule_id: The FUP rule UUID.
            **kwargs: Fields to update.

        Returns:
            The updated FupRule.
        """
        rule = FupPolicies._get_rule(db, rule_id)
        enum_fields = {
            "consumption_period": FupConsumptionPeriod,
            "direction": FupDirection,
            "threshold_unit": FupDataUnit,
            "action": FupAction,
        }
        allowed_fields = {
            "name",
            "sort_order",
            "consumption_period",
            "direction",
            "threshold_amount",
            "threshold_unit",
            "action",
            "speed_reduction_percent",
            "time_start",
            "time_end",
            "enabled_by_rule_id",
            "days_of_week",
            "is_active",
        }
        for key, value in kwargs.items():
            if key not in allowed_fields:
                continue
            if key in enum_fields and value is not None:
                value = validate_enum(value, enum_fields[key], key)
            if key == "name" and isinstance(value, str):
                value = value.strip()
            if key == "enabled_by_rule_id":
                value = coerce_uuid(value) if value else None
            setattr(rule, key, value)
        rule.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(rule)
        logger.info("Updated FUP rule %s", rule_id)
        return rule

    @staticmethod
    def delete_rule(db: Session, rule_id: str) -> None:
        """Permanently delete an FUP rule.

        Args:
            db: Database session.
            rule_id: The FUP rule UUID.
        """
        rule = FupPolicies._get_rule(db, rule_id)
        policy_id = rule.policy_id
        db.delete(rule)
        db.commit()
        logger.info("Deleted FUP rule %s from policy %s", rule_id, policy_id)

    @staticmethod
    def list_rules(db: Session, policy_id: str) -> list[FupRule]:
        """List all rules for a given FUP policy, ordered by sort_order.

        Args:
            db: Database session.
            policy_id: The FUP policy UUID.

        Returns:
            List of FupRule objects.
        """
        stmt = (
            select(FupRule)
            .where(FupRule.policy_id == coerce_uuid(policy_id))
            .order_by(FupRule.sort_order.asc())
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def clone_rules_from(
        db: Session,
        source_offer_id: str,
        target_policy_id: str,
    ) -> list[FupRule]:
        """Copy all FUP rules from another offer's policy into the target policy.

        Args:
            db: Database session.
            source_offer_id: The offer UUID whose FUP rules to copy.
            target_policy_id: The target FUP policy UUID to copy rules into.

        Returns:
            List of newly created FupRule copies.
        """
        source_policy = FupPolicies.get_by_offer(db, source_offer_id)
        if not source_policy:
            raise HTTPException(
                status_code=404,
                detail="Source offer has no FUP policy",
            )
        target_policy = FupPolicies._get_policy(db, target_policy_id)

        cloned: list[FupRule] = []
        rule_map: dict[str, FupRule] = {}
        for source_rule in source_policy.rules:
            rule = FupRule(
                policy_id=target_policy.id,
                name=source_rule.name,
                sort_order=source_rule.sort_order,
                consumption_period=source_rule.consumption_period,
                direction=source_rule.direction,
                threshold_amount=source_rule.threshold_amount,
                threshold_unit=source_rule.threshold_unit,
                action=source_rule.action,
                speed_reduction_percent=source_rule.speed_reduction_percent,
                cooldown_minutes=source_rule.cooldown_minutes,
                time_start=source_rule.time_start,
                time_end=source_rule.time_end,
                days_of_week=list(source_rule.days_of_week)
                if source_rule.days_of_week
                else None,
                is_active=source_rule.is_active,
            )
            db.add(rule)
            cloned.append(rule)
            rule_map[str(source_rule.id)] = rule
        db.flush()
        for source_rule in source_policy.rules:
            cloned_rule = rule_map[str(source_rule.id)]
            if source_rule.enabled_by_rule_id:
                cloned_rule.enabled_by_rule_id = rule_map[
                    str(source_rule.enabled_by_rule_id)
                ].id
        db.commit()
        for rule in cloned:
            db.refresh(rule)
        logger.info(
            "Cloned %d FUP rules from offer %s to policy %s",
            len(cloned),
            source_offer_id,
            target_policy_id,
        )
        return cloned


fup_policies = FupPolicies()


# ═══════════════════════════════════════════════════════════════════
# FUP Rule Evaluation & Simulation Engine
# ═══════════════════════════════════════════════════════════════════


def _threshold_gb(rule: FupRule) -> float:
    """Convert threshold to GB for comparison."""
    amount = rule.threshold_amount or 0
    unit = rule.threshold_unit.value if rule.threshold_unit else "gb"
    return {"mb": amount / 1024, "gb": amount, "tb": amount * 1024}.get(unit, amount)


def _time_in_window(
    check_time: datetime | None,
    start: time | None,
    end: time | None,
    inverse: bool = False,
) -> bool:
    """Check if a time falls within a start-end window.

    If inverse=True, returns True when OUTSIDE the window (e.g., night browsing
    is "free" = traffic inside window doesn't count, so inverse window is "counted").
    """
    if start is None or end is None:
        return True  # no window = always applies
    if check_time is None:
        return True

    t = check_time.time()

    if start <= end:
        in_window = start <= t <= end
    else:
        # Overnight window (e.g., 22:00 → 06:00)
        in_window = t >= start or t <= end

    return not in_window if inverse else in_window


def _day_in_list(check_time: datetime | None, days: list[int] | None) -> bool:
    """Check if a datetime's weekday is in the allowed days list (0=Mon..6=Sun)."""
    if not days:
        return True  # no filter = all days
    if check_time is None:
        return True
    return check_time.weekday() in days


def evaluate_rules(
    db: Session,
    offer_id: str,
    *,
    current_usage_gb: float,
    current_time: datetime | None = None,
    fired_rule_ids: set | None = None,
) -> list[dict]:
    """Evaluate FUP rules for an offer against current usage.

    Returns a list of rule evaluation results (in sort_order):
    - rule_id, name, sort_order
    - threshold_gb, triggered (bool)
    - action, speed_reduction_percent
    - reason: why triggered or skipped
    - blocked_by: rule that needs to fire first (if chained)

    This is the core engine used by both simulation and real enforcement.
    """
    policy = FupPolicies.get_by_offer(db, offer_id)
    if not policy or not policy.is_active:
        return []

    current_time = current_time or datetime.now(UTC)
    fired_rule_ids = fired_rule_ids or set()

    results: list[dict] = []

    for rule in policy.rules:
        if not rule.is_active:
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": _threshold_gb(rule),
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": "Rule is disabled",
                    "status": "disabled",
                }
            )
            continue

        threshold = _threshold_gb(rule)

        # Check chaining: does this rule require a prior rule to have fired?
        if (
            rule.enabled_by_rule_id
            and str(rule.enabled_by_rule_id) not in fired_rule_ids
        ):
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": threshold,
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": "Waiting for prerequisite rule to fire",
                    "blocked_by": str(rule.enabled_by_rule_id),
                    "status": "waiting",
                }
            )
            continue

        # Check time-of-day window (rule-level overrides policy-level)
        time_start = rule.time_start or policy.traffic_accounting_start
        time_end = rule.time_end or policy.traffic_accounting_end
        inverse = policy.traffic_inverse_interval

        if not _time_in_window(current_time, time_start, time_end, inverse):
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": threshold,
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": f"Outside time window ({time_start}-{time_end})",
                    "status": "time_skip",
                }
            )
            continue

        # Check day-of-week
        days = rule.days_of_week or policy.traffic_days_of_week
        if not _day_in_list(current_time, days):
            day_names = {
                0: "Mon",
                1: "Tue",
                2: "Wed",
                3: "Thu",
                4: "Fri",
                5: "Sat",
                6: "Sun",
            }
            allowed = ", ".join(day_names.get(d, str(d)) for d in (days or []))
            results.append(
                {
                    "rule_id": str(rule.id),
                    "name": rule.name,
                    "sort_order": rule.sort_order,
                    "threshold_gb": threshold,
                    "triggered": False,
                    "action": rule.action.value,
                    "speed_reduction_percent": rule.speed_reduction_percent,
                    "reason": f"Not active today (active: {allowed})",
                    "status": "day_skip",
                }
            )
            continue

        # Check threshold
        triggered = current_usage_gb >= threshold

        if triggered:
            fired_rule_ids.add(str(rule.id))

        results.append(
            {
                "rule_id": str(rule.id),
                "name": rule.name,
                "sort_order": rule.sort_order,
                "threshold_gb": threshold,
                "current_usage_gb": round(current_usage_gb, 2),
                "triggered": triggered,
                "action": rule.action.value if triggered else None,
                "speed_reduction_percent": rule.speed_reduction_percent
                if triggered
                else None,
                "reason": (
                    f"Usage {current_usage_gb:.1f} GB >= threshold {threshold:.1f} GB"
                    if triggered
                    else f"Usage {current_usage_gb:.1f} GB < threshold {threshold:.1f} GB"
                ),
                "status": "triggered" if triggered else "ok",
                "usage_percent": round(current_usage_gb / threshold * 100, 1)
                if threshold > 0
                else 0,
            }
        )

    return results


def simulate_fup(
    db: Session,
    offer_id: str,
    *,
    current_usage_gb: float = 0,
    current_time: datetime | None = None,
    current_day: int | None = None,
    billing_day_elapsed: int = 15,
    billing_cycle_days: int = 30,
) -> dict:
    """Simulate FUP rule evaluation for a given scenario.

    Returns a complete simulation result with:
    - rules: list of rule evaluation results
    - summary: overall status (ok/warning/throttled/blocked)
    - projection: estimated usage at end of cycle
    """
    if current_time is None:
        current_time = datetime.now(UTC)
    if current_day is not None:
        # Override the day-of-week for simulation
        # Find next date with the target weekday
        while current_time.weekday() != current_day:
            from datetime import timedelta

            current_time += timedelta(days=1)

    rules = evaluate_rules(
        db,
        offer_id,
        current_usage_gb=current_usage_gb,
        current_time=current_time,
    )

    # Calculate projection
    daily_avg = current_usage_gb / max(billing_day_elapsed, 1)
    projected_total = daily_avg * billing_cycle_days
    days_remaining = max(0, billing_cycle_days - billing_day_elapsed)

    # Determine overall status
    triggered_rules = [r for r in rules if r.get("triggered")]
    if any(r["action"] == "block" for r in triggered_rules):
        status = "blocked"
    elif any(r["action"] == "reduce_speed" for r in triggered_rules):
        status = "throttled"
    elif any(r["action"] == "notify" for r in triggered_rules):
        status = "warning"
    else:
        status = "ok"

    # Find the highest threshold for progress calculation
    max_threshold = max((r["threshold_gb"] for r in rules), default=0)

    return {
        "rules": rules,
        "summary": {
            "status": status,
            "triggered_count": len(triggered_rules),
            "total_rules": len(rules),
            "current_usage_gb": round(current_usage_gb, 2),
            "max_threshold_gb": round(max_threshold, 2),
            "usage_percent": round(current_usage_gb / max_threshold * 100, 1)
            if max_threshold > 0
            else 0,
        },
        "projection": {
            "daily_average_gb": round(daily_avg, 2),
            "projected_total_gb": round(projected_total, 2),
            "days_remaining": days_remaining,
            "will_exceed": projected_total > max_threshold
            if max_threshold > 0
            else False,
            "days_until_cap": round((max_threshold - current_usage_gb) / daily_avg, 1)
            if daily_avg > 0 and max_threshold > current_usage_gb
            else 0,
        },
    }
