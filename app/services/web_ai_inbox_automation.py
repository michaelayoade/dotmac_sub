"""Admin form adapter for default-off AI inbox automation policy."""

from __future__ import annotations

import json
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.schemas.ai_operations import AiIntakeConfigUpsert
from app.services import ai_inbox_automation, ai_operations
from app.services.ai_inbox_automation import DEFAULT_SCOPE_KEY

HandoffPolicyValue = Literal["manual_review", "live_agent", "close_only"]
AssignmentStrategyValue = Literal["available_round_robin", "queue_only"]


def _json_list(text: str | None, *, field_name: str) -> list:
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON.") from exc
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array.")
    return value


def _uuid_or_none(value: str | None) -> UUID | None:
    text = str(value or "").strip()
    if not text:
        return None
    return UUID(text)


def _handoff_policy(value: str) -> HandoffPolicyValue:
    if value not in {"manual_review", "live_agent", "close_only"}:
        raise ValueError("Handoff policy is invalid.")
    return cast(HandoffPolicyValue, value)


def _assignment_strategy(value: str) -> AssignmentStrategyValue:
    if value not in {"available_round_robin", "queue_only"}:
        raise ValueError("Assignment strategy is invalid.")
    return cast(AssignmentStrategyValue, value)


def _team_options(db: Session) -> list[dict[str, str]]:
    rows = (
        db.query(ServiceTeam)
        .filter(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name.asc())
        .all()
    )
    return [{"id": str(row.id), "label": row.name} for row in rows]


def build_config_state(db: Session, *, scope_key: str = DEFAULT_SCOPE_KEY) -> dict:
    config = ai_inbox_automation.get_config(db, scope_key=scope_key)
    policy = ai_inbox_automation.policy_from_config(config)
    effective = ai_inbox_automation.effective_state(db, scope_key=scope_key)
    workflow_json = [
        {
            "position": step.position,
            "action": step.action.value,
            "prompt": step.prompt,
            "required_context": [source.value for source in step.required_context],
            "handoff_on_failure": step.handoff_on_failure,
        }
        for step in policy.workflow_steps
    ]
    mappings_json = [
        {
            "intent": row.intent,
            "service_team_id": str(row.service_team_id)
            if row.service_team_id
            else None,
            "label": row.label,
        }
        for row in policy.department_mappings
    ]
    return {
        "scope_key": policy.scope_key,
        "policy": policy,
        "effective": effective,
        "context_source_options": ai_inbox_automation.context_source_options(),
        "workflow_action_options": [
            item.value for item in ai_inbox_automation.WorkflowAction
        ],
        "channel_options": [item.value for item in ai_inbox_automation.IntakeChannel],
        "handoff_policy_options": [
            item.value for item in ai_inbox_automation.HandoffPolicy
        ],
        "assignment_strategy_options": [
            item.value for item in ai_inbox_automation.AssignmentStrategy
        ],
        "service_team_options": _team_options(db),
        "form": {
            "scope_key": policy.scope_key,
            "channel_type": policy.channel_type.value,
            "is_enabled": policy.is_enabled,
            "auto_reply_enabled": policy.auto_reply_enabled,
            "auto_handoff_enabled": policy.auto_handoff_enabled,
            "confidence_threshold": policy.confidence_threshold,
            "allow_followup_questions": policy.allow_followup_questions,
            "max_clarification_turns": policy.max_clarification_turns,
            "escalate_after_minutes": policy.escalate_after_minutes,
            "fallback_team_id": str(policy.fallback_team_id)
            if policy.fallback_team_id
            else "",
            "handoff_policy": policy.handoff_policy.value,
            "assignment_strategy": policy.assignment_strategy.value,
            "instructions": policy.instructions or "",
            "context_sources": [item.value for item in policy.context_sources],
            "department_mappings_json": json.dumps(mappings_json, indent=2),
            "workflow_steps_json": json.dumps(workflow_json, indent=2),
        },
    }


def save_config(
    db: Session,
    *,
    scope_key: str,
    channel_type: str,
    is_enabled: bool,
    auto_reply_enabled: bool,
    auto_handoff_enabled: bool,
    confidence_threshold: str,
    allow_followup_questions: bool,
    max_clarification_turns: str,
    escalate_after_minutes: str,
    fallback_team_id: str | None,
    handoff_policy: str,
    assignment_strategy: str,
    instructions: str | None,
    context_sources: list[str],
    department_mappings_json: str | None,
    workflow_steps_json: str | None,
):
    payload = AiIntakeConfigUpsert(
        scope_key=scope_key,
        channel_type=channel_type,
        is_enabled=is_enabled,
        auto_reply_enabled=auto_reply_enabled,
        auto_handoff_enabled=auto_handoff_enabled,
        confidence_threshold=float(confidence_threshold or 0.75),
        allow_followup_questions=allow_followup_questions,
        max_clarification_turns=int(max_clarification_turns or 1),
        escalate_after_minutes=int(escalate_after_minutes or 5),
        fallback_team_id=_uuid_or_none(fallback_team_id),
        instructions=str(instructions or "").strip() or None,
        context_sources=context_sources,
        department_mappings=_json_list(
            department_mappings_json, field_name="Department mappings"
        ),
        workflow_steps=_json_list(workflow_steps_json, field_name="Workflow steps"),
        handoff_policy=_handoff_policy(handoff_policy),
        assignment_strategy=_assignment_strategy(assignment_strategy),
        metadata={
            "source": "admin_inbox_automation",
            "consumer_state": "wired_disabled_by_default",
        },
    )
    return ai_operations.upsert_intake_config_committed(db, payload)
