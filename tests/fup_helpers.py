"""Typed FUP command helpers for behavior tests."""

from __future__ import annotations

from datetime import time
from uuid import uuid4

from sqlalchemy.orm import Session

from app.services.db_session_adapter import db_session_adapter
from app.services.fup import (
    AddFupRuleCommand,
    CloneFupRulesCommand,
    EnsureFupPolicyCommand,
    FupRuleSpec,
    fup_policies,
)
from app.services.owner_commands import CommandContext


def execute_owner_command_for_test(
    db: Session,
    *,
    operation,
    **_kwargs,
):
    """Exercise a staged owner operation with the production commit contract."""
    try:
        result = operation()
    except Exception:
        db.rollback()
        raise
    db.commit()
    return result


def fup_command_context(scope: str, reason: str = "test_fup_command") -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="test:fup",
        scope=scope,
        reason=reason,
    )


def ensure_fup_policy(db: Session, offer_id: str):
    command = EnsureFupPolicyCommand(
        context=fup_command_context(offer_id, "test_ensure_fup_policy"),
        offer_id=offer_id,
    )
    db_session_adapter.release_read_transaction(db)
    return fup_policies.ensure(db, command)


def add_fup_rule(
    db: Session,
    offer_id: str,
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
    cooldown_minutes: int = 0,
    days_of_week: list[int] | None = None,
    is_active: bool = True,
):
    command = AddFupRuleCommand(
        context=fup_command_context(offer_id, "test_add_fup_rule"),
        offer_id=offer_id,
        spec=FupRuleSpec(
            name=name,
            consumption_period=consumption_period,
            direction=direction,
            threshold_amount=threshold_amount,
            threshold_unit=threshold_unit,
            action=action,
            speed_reduction_percent=speed_reduction_percent,
            sort_order=sort_order,
            time_start=time_start,
            time_end=time_end,
            enabled_by_rule_id=enabled_by_rule_id,
            cooldown_minutes=cooldown_minutes,
            days_of_week=days_of_week,
            is_active=is_active,
        ),
    )
    db_session_adapter.release_read_transaction(db)
    return fup_policies.add_rule(db, command)


def clone_fup_rules(db: Session, source_offer_id: str, target_offer_id: str):
    command = CloneFupRulesCommand(
        context=fup_command_context(target_offer_id, "test_clone_fup_rules"),
        source_offer_id=source_offer_id,
        target_offer_id=target_offer_id,
    )
    db_session_adapter.release_read_transaction(db)
    return fup_policies.clone_rules(db, command)
