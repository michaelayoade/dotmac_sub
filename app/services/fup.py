"""Fair Usage Policy service for traffic-based speed reduction policies."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
            sort_order=next_order,
            consumption_period=validate_enum(
                consumption_period, FupConsumptionPeriod, "consumption_period"
            ),
            direction=validate_enum(direction, FupDirection, "direction"),
            threshold_amount=threshold_amount,
            threshold_unit=validate_enum(threshold_unit, FupDataUnit, "threshold_unit"),
            action=validate_enum(action, FupAction, "action"),
            speed_reduction_percent=speed_reduction_percent,
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
            "is_active",
        }
        for key, value in kwargs.items():
            if key not in allowed_fields:
                continue
            if key in enum_fields and value is not None:
                value = validate_enum(value, enum_fields[key], key)
            if key == "name" and isinstance(value, str):
                value = value.strip()
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
                is_active=source_rule.is_active,
            )
            db.add(rule)
            cloned.append(rule)
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
