"""Team Inbox side effects for configured AI intake decisions."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamSource,
)
from app.services import (
    ai_inbox_automation,
    team_inbox_assignment,
    team_inbox_outbound,
)
from app.services.ai.client import AIClientError


@dataclass(frozen=True, slots=True)
class InboxAIAutomationOutcome:
    kind: str
    reason: str | None = None
    intent: str | None = None
    target_service_team_id: str | None = None
    assigned_person_id: str | None = None
    reply_kind: str | None = None


def _metadata(conversation: InboxConversation) -> dict:
    return dict(conversation.metadata_ or {})


def _record_decision(
    conversation: InboxConversation,
    *,
    status: str,
    decision: ai_inbox_automation.IntakeDecision | None = None,
    reason: str | None = None,
    observation_id: UUID | None = None,
    message_id: UUID | None = None,
    action: str | None = None,
    assignment_kind: str | None = None,
    assigned_person_id: str | None = None,
    reply_kind: str | None = None,
) -> None:
    metadata = _metadata(conversation)
    metadata["last_ai_intake"] = {
        "status": status,
        "reason": reason,
        "action": action,
        "observation_id": str(observation_id) if observation_id else None,
        "message_id": str(message_id) if message_id else None,
        "intent": decision.intent if decision else None,
        "confidence": decision.confidence if decision else None,
        "has_enough_information": decision.has_enough_information if decision else None,
        "should_handoff": decision.should_handoff if decision else None,
        "target_service_team_id": str(decision.target_service_team_id)
        if decision and decision.target_service_team_id
        else None,
        "should_close": decision.should_close if decision else None,
        "rationale": decision.rationale if decision else None,
        "assignment_kind": assignment_kind,
        "assigned_person_id": assigned_person_id,
        "reply_kind": reply_kind,
        "at": datetime.now(UTC).isoformat(),
    }
    conversation.metadata_ = metadata


def _send_customer_reply(
    db: Session,
    *,
    conversation: InboxConversation,
    body: str,
    message_id: UUID,
    reply_type: str,
) -> team_inbox_outbound.InboxReplyResult:
    clean_body = body.strip()
    return team_inbox_outbound.send_inbox_reply(
        db,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_text=clean_body,
            body_html=f"<p>{html.escape(clean_body)}</p>",
            metadata={
                "source": "ai_inbox_intake",
                "reply_type": reply_type,
                "inbound_message_id": str(message_id),
            },
        ),
        record_failure=True,
    )


def _handoff(
    db: Session,
    *,
    conversation: InboxConversation,
    policy: ai_inbox_automation.IntakePolicy,
    service_team_id: UUID,
    reason: str,
) -> team_inbox_assignment.InboxAssignmentResult:
    if policy.assignment_strategy == ai_inbox_automation.AssignmentStrategy.queue_only:
        return team_inbox_assignment.queue_conversation_for_team(
            db,
            conversation=conversation,
            service_team_id=service_team_id,
            reason=reason,
            source=InboxTeamSource.routing_rule.value,
        )
    return team_inbox_assignment.assign_conversation_to_available_agent(
        db,
        conversation=conversation,
        service_team_id=service_team_id,
        reason=reason,
        source=InboxTeamSource.routing_rule.value,
    )


def _resolve_conversation(
    conversation: InboxConversation, *, reason: str, message_id: UUID
) -> None:
    if conversation.status == InboxConversationStatus.resolved.value:
        return
    metadata = _metadata(conversation)
    history = metadata.get("status_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": conversation.status,
            "to": InboxConversationStatus.resolved.value,
            "at": datetime.now(UTC).isoformat(),
            "source": "ai_inbox_intake",
            "reason": reason,
            "message_id": str(message_id),
        }
    )
    metadata["status_history"] = history[-50:]
    conversation.status = InboxConversationStatus.resolved.value
    conversation.metadata_ = metadata


def apply_inbound_ai_intake(
    db: Session,
    *,
    conversation_id: UUID,
    message_id: UUID,
    observation_id: UUID | None = None,
) -> InboxAIAutomationOutcome:
    state = ai_inbox_automation.effective_state(db)
    if not state.may_classify:
        return InboxAIAutomationOutcome(kind="skipped", reason=state.reason)

    config = ai_inbox_automation.get_config(db)
    policy = ai_inbox_automation.policy_from_config(config)
    conversation = db.get(InboxConversation, conversation_id)
    message = db.get(InboxMessage, message_id)
    if conversation is None or message is None:
        return InboxAIAutomationOutcome(kind="skipped", reason="missing_record")
    if message.direction != InboxMessageDirection.inbound.value:
        return InboxAIAutomationOutcome(kind="skipped", reason="not_inbound")
    if not conversation.is_active:
        return InboxAIAutomationOutcome(kind="skipped", reason="inactive")

    context = ai_inbox_automation.conversation_context(
        db, conversation_id=conversation.id, policy=policy
    )
    try:
        decision = ai_inbox_automation.classify_intake(
            db,
            policy=policy,
            context=context,
            inbound_body=message.body or "",
        )
    except AIClientError as exc:
        _record_decision(
            conversation,
            status="failed",
            reason=str(exc)[:300],
            observation_id=observation_id,
            message_id=message.id,
            action="classification_failed",
        )
        db.flush()
        return InboxAIAutomationOutcome(kind="failed", reason="classification_failed")

    confident = decision.confidence >= policy.confidence_threshold
    reply_result: team_inbox_outbound.InboxReplyResult | None = None
    assignment_result: team_inbox_assignment.InboxAssignmentResult | None = None
    action = "classified"

    needs_followup = not confident or not decision.has_enough_information
    if (
        needs_followup
        and policy.allow_followup_questions
        and state.may_send_customer_reply
        and decision.followup_question
    ):
        reply_result = _send_customer_reply(
            db,
            conversation=conversation,
            body=decision.followup_question,
            message_id=message.id,
            reply_type="clarification",
        )
        action = "asked_followup"
    elif (
        state.may_handoff
        and policy.handoff_policy == ai_inbox_automation.HandoffPolicy.live_agent
        and decision.should_handoff
        and decision.target_service_team_id is not None
    ):
        assignment_result = _handoff(
            db,
            conversation=conversation,
            policy=policy,
            service_team_id=decision.target_service_team_id,
            reason=f"ai_intake:{decision.intent}",
        )
        action = "handed_off"
    elif state.may_send_customer_reply and decision.customer_reply:
        reply_result = _send_customer_reply(
            db,
            conversation=conversation,
            body=decision.customer_reply,
            message_id=message.id,
            reply_type="answer",
        )
        action = "replied"
    elif (
        state.may_handoff
        and policy.handoff_policy == ai_inbox_automation.HandoffPolicy.close_only
        and decision.should_close
    ):
        _resolve_conversation(
            conversation,
            reason=f"ai_intake:{decision.intent}",
            message_id=message.id,
        )
        action = "closed"

    _record_decision(
        conversation,
        status="applied",
        decision=decision,
        observation_id=observation_id,
        message_id=message.id,
        action=action,
        assignment_kind=assignment_result.kind if assignment_result else None,
        assigned_person_id=assignment_result.assigned_person_id
        if assignment_result
        else None,
        reply_kind=reply_result.kind if reply_result else None,
    )
    db.flush()
    return InboxAIAutomationOutcome(
        kind=action,
        intent=decision.intent,
        target_service_team_id=str(decision.target_service_team_id)
        if decision.target_service_team_id
        else None,
        assigned_person_id=assignment_result.assigned_person_id
        if assignment_result
        else None,
        reply_kind=reply_result.kind if reply_result else None,
    )
