"""Disable unsafe first-turn AI intake handoff rules.

Revision ID: 566_ai_intake_disable_first_turn_handoff_rules
Revises: 565_erp_department_service_team_membership
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "566_ai_intake_disable_first_turn_handoff_rules"
down_revision: str | None = "565_erp_department_service_team_membership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPAIR_EVENT = "ai_intake_disable_first_turn_handoff_rules"


def _turn_count_matches_first_turn(condition: Mapping[str, Any]) -> bool:
    if str(condition.get("type") or "").strip() != "turn_count":
        return False
    try:
        expected = int(str(condition.get("turn_count", condition.get("value", 0))))
    except (TypeError, ValueError):
        return False
    operator = str(condition.get("operator") or ">=").strip()
    if operator == ">=":
        return 1 >= expected
    if operator == ">":
        return 1 > expected
    if operator in {"=", "==", "equals"}:
        return 1 == expected
    if operator == "<=":
        return 1 <= expected
    if operator == "<":
        return 1 < expected
    return False


def _disable_unsafe_rules(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    updated = dict(metadata)
    raw_policy = updated.get("conversation_policy")
    if not isinstance(raw_policy, Mapping):
        return None
    policy = dict(raw_policy)
    raw_rules = policy.get("troubleshooting_rules")
    if not isinstance(raw_rules, list):
        return None

    changed = False
    rules: list[Any] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            rules.append(raw_rule)
            continue
        rule = dict(raw_rule)
        condition = rule.get("condition")
        if (
            rule.get("enabled") is not False
            and str(rule.get("action") or "").strip() == "handoff"
            and isinstance(condition, Mapping)
            and _turn_count_matches_first_turn(condition)
        ):
            rule["enabled"] = False
            rule["disabled_reason"] = REPAIR_EVENT
            changed = True
        rules.append(rule)

    if not changed:
        return None

    policy["troubleshooting_rules"] = rules
    updated["conversation_policy"] = policy
    events = updated.get("policy_repair_events")
    repair_events = list(events) if isinstance(events, list) else []
    repair_events.append(
        {
            "event": REPAIR_EVENT,
            "applied_at": datetime.now(UTC).isoformat(),
            "scope": "active_policy_versions",
        }
    )
    updated["policy_repair_events"] = repair_events[-20:]
    return updated


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "ai_intake_policy_versions" not in tables:
        return

    update_stmt = sa.text(
        """
        UPDATE ai_intake_policy_versions
        SET metadata = :metadata
        WHERE id = :id
        """
    ).bindparams(sa.bindparam("metadata", type_=sa.JSON()))
    rows = bind.execute(
        sa.text(
            """
            SELECT id, metadata
            FROM ai_intake_policy_versions
            WHERE is_active = true
              AND metadata IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        metadata = row["metadata"]
        if not isinstance(metadata, Mapping):
            continue
        repaired = _disable_unsafe_rules(metadata)
        if repaired is None:
            continue
        bind.execute(update_stmt, {"id": row["id"], "metadata": repaired})


def downgrade() -> None:
    # The migration disables unsafe production policy rules. Re-enabling them on
    # downgrade would deliberately restore a customer-visible bad route.
    return
