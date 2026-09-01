"""Read-only manager AI assistant for Team Inbox conversation insight."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.team_inbox import InboxChannelType, InboxConversationStatus
from app.services import control_registry
from app.services import team_inbox_analysis_projection as analysis_projection
from app.services.ai.client import AIClientError
from app.services.ai.gateway import ai_gateway
from app.services.ai.security import redact_secret_text
from app.services.workqueue.scope import WorkqueueScope

MANAGER_AI_PERMISSION = "support:inbox_ai:read"
_MAX_QUESTION_CHARS = 2000


@dataclass(frozen=True, slots=True)
class ManagerChatConversationOption:
    id: UUID
    label: str
    status: str
    channel_type: str
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class ManagerChatFilterOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ManagerChatPageState:
    conversations: tuple[ManagerChatConversationOption, ...]
    selected_conversation_id: UUID | None
    question: str
    answer: str | None
    error: str | None
    provider_enabled: bool
    generation_enabled: bool
    mode: str
    period: str
    custom_start: str
    custom_end: str
    channel_type: str
    status_filter: str
    channel_options: tuple[ManagerChatFilterOption, ...]
    status_options: tuple[ManagerChatFilterOption, ...]
    period_facts: analysis_projection.ManagerPeriodFacts | None


def _coerce_uuid(value: object) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _filter_label(value: str) -> str:
    labels = {
        "whatsapp": "WhatsApp",
        "instagram_dm": "Instagram DM",
    }
    if value in labels:
        return labels[value]
    return value.replace("_", " ").title()


def _channel_options() -> tuple[ManagerChatFilterOption, ...]:
    return tuple(
        ManagerChatFilterOption(value=item.value, label=_filter_label(item.value))
        for item in InboxChannelType
        if item is not InboxChannelType.note
    )


def _status_options() -> tuple[ManagerChatFilterOption, ...]:
    return tuple(
        ManagerChatFilterOption(value=item.value, label=_filter_label(item.value))
        for item in InboxConversationStatus
    )


def _clean_channel_type(value: str | None) -> str | None:
    text = str(value or "").strip()
    allowed = {
        item.value for item in InboxChannelType if item is not InboxChannelType.note
    }
    return text if text in allowed else None


def _clean_status(value: str | None) -> str | None:
    text = str(value or "").strip()
    allowed = {item.value for item in InboxConversationStatus}
    return text if text in allowed else None


def _custom_date(value: str | None) -> date | None:
    text = str(value or "").strip()
    return datetime.fromisoformat(text).date() if text else None


def _conversation_label(
    conversation: analysis_projection.ManagerEvidenceConversation,
) -> str:
    parts = [
        conversation.subject,
        conversation.channel_type,
    ]
    label = " - ".join(str(part).strip() for part in parts if str(part or "").strip())
    return label[:180] or str(conversation.id)


def recent_conversation_options(
    db: Session, *, scope: WorkqueueScope, selected_conversation_id: UUID | None = None
) -> tuple[ManagerChatConversationOption, ...]:
    projection = analysis_projection.build_projection(
        db,
        analysis_projection.ManagerAnalysisRequest(
            scope=scope, mode=analysis_projection.ManagerAnalysisMode.recent_queue
        ),
    )
    rows = list(projection.recent_conversations)
    if selected_conversation_id and all(
        row.id != selected_conversation_id for row in rows
    ):
        selected_projection = analysis_projection.build_projection(
            db,
            analysis_projection.ManagerAnalysisRequest(
                scope=scope,
                mode=analysis_projection.ManagerAnalysisMode.conversation,
                conversation_id=selected_conversation_id,
            ),
        )
        if selected_projection.selected_conversation is not None:
            rows.insert(0, selected_projection.selected_conversation)
    return tuple(
        ManagerChatConversationOption(
            id=row.id,
            label=_conversation_label(row),
            status=row.current_status,
            channel_type=row.channel_type,
            last_message_at=row.activity_at,
        )
        for row in rows
    )


def _message_payload(
    message: analysis_projection.ManagerEvidenceMessage,
) -> dict[str, Any]:
    return {
        "direction": message.direction,
        "body": message.body,
        "occurred_at": message.occurred_at.isoformat(),
    }


def _conversation_payload(
    conversation: analysis_projection.ManagerEvidenceConversation | None,
) -> dict[str, Any] | None:
    if conversation is None:
        return None
    return {
        "id": str(conversation.id),
        "channel_type": conversation.channel_type,
        "status": conversation.current_status,
        "subject": conversation.subject,
        "activity_at": conversation.activity_at.isoformat(),
        "reasons": conversation.reasons,
        "messages": [_message_payload(message) for message in conversation.messages],
    }


def _queue_payload(
    projection: analysis_projection.ManagerAnalysisProjection,
) -> dict[str, Any]:
    return {
        "recent_conversations": [
            {
                "id": str(row.id),
                "label": _conversation_label(row),
                "status": row.current_status,
                "channel_type": row.channel_type,
                "last_message_at": row.activity_at.isoformat(),
            }
            for row in projection.recent_conversations
        ]
    }


def _system_prompt() -> str:
    return (
        "You are a manager-only Team Inbox analyst. Answer from the provided "
        "inbox context only. Give operational insight about customer intent, "
        "urgency, missing information, risk, and recommended manager follow-up. "
        "Treat period facts as deterministic. In a period review, distinguish "
        "current state of the historical cohort from transitions that happened "
        "during that period, and state that qualitative findings use only the "
        "reported evidence sample. Themes, frustration, and overlooked context "
        "are AI observations, not canonical facts. "
        "Do not claim you performed assignments, replies, refunds, profile "
        "updates, or service changes. If context is insufficient, say what is "
        "missing. Keep the answer concise."
    )


def answer_manager_question(
    db: Session,
    *,
    scope: WorkqueueScope,
    question: str,
    mode: str = "recent_queue",
    conversation_id: UUID | str | None = None,
    period: str = "last_7_days",
    custom_start: str | None = None,
    custom_end: str | None = None,
    channel_type: str | None = None,
    status: str | None = None,
) -> str:
    clean_question = str(question or "").strip()
    if not clean_question:
        raise ValueError("Question is required.")
    if len(clean_question) > _MAX_QUESTION_CHARS:
        raise ValueError("Question is too long.")
    if not control_registry.is_enabled(db, "ai.generation"):
        raise AIClientError("AI generation is disabled", failure_type="ai_disabled")
    if not ai_gateway.enabled(db):
        raise AIClientError("AI provider is disabled", failure_type="ai_disabled")

    selected_id = _coerce_uuid(conversation_id)
    try:
        requested_mode = analysis_projection.ManagerAnalysisMode(mode)
        requested_period = analysis_projection.ManagerAnalysisPeriod(period)
        start_date = (
            _custom_date(custom_start)
            if requested_period is analysis_projection.ManagerAnalysisPeriod.custom
            else None
        )
        end_date = (
            _custom_date(custom_end)
            if requested_period is analysis_projection.ManagerAnalysisPeriod.custom
            else None
        )
    except ValueError as exc:
        raise ValueError("Invalid Manager AI analysis filters.") from exc
    projection = analysis_projection.build_projection(
        db,
        analysis_projection.ManagerAnalysisRequest(
            scope=scope,
            mode=requested_mode,
            conversation_id=selected_id,
            period=requested_period,
            custom_start=start_date,
            custom_end=end_date,
            channel_type=_clean_channel_type(channel_type),
            status=_clean_status(status),
        ),
    )
    payload = {
        "question": clean_question,
        "mode": projection.mode.value,
        "conversation": _conversation_payload(projection.selected_conversation),
        "queue": _queue_payload(projection)
        if projection.mode is analysis_projection.ManagerAnalysisMode.recent_queue
        else None,
        "period_facts": (
            {
                "period_start": projection.facts.period_start.isoformat(),
                "period_end": projection.facts.period_end.isoformat(),
                "timezone": projection.facts.timezone,
                "cohort_definition": projection.facts.cohort_definition,
                "total_conversations": projection.facts.total_conversations,
                "current_state_status_counts": projection.facts.current_state_status_counts,
                "channel_counts": projection.facts.channel_counts,
                "resolved_transition_count": projection.facts.resolved_transition_count,
                "reopened_conversation_ids": [
                    str(value) for value in projection.facts.reopened_conversation_ids
                ],
                "escalated_conversation_ids": [
                    str(value) for value in projection.facts.escalated_conversation_ids
                ],
                "ai_evidence_count": projection.facts.evidence_count,
            }
            if projection.facts
            else None
        ),
        "evidence_conversations": [
            _conversation_payload(value) for value in projection.evidence_conversations
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }

    response, _routing = ai_gateway.generate_with_fallback(
        db,
        system=_system_prompt(),
        prompt=json.dumps(payload, sort_keys=True, default=str),
        max_tokens=900,
    )
    return (response.content or "").strip() or "No answer was generated."


def build_page_state(
    db: Session,
    *,
    scope: WorkqueueScope,
    conversation_id: UUID | str | None = None,
    question: str | None = None,
    answer: str | None = None,
    error: str | None = None,
    mode: str = "recent_queue",
    period: str = "last_7_days",
    custom_start: str | None = None,
    custom_end: str | None = None,
    channel_type: str | None = None,
    status: str | None = None,
) -> ManagerChatPageState:
    selected_id = _coerce_uuid(conversation_id)
    period_facts = None
    clean_channel_type = _clean_channel_type(channel_type)
    clean_status = _clean_status(status)
    if mode == analysis_projection.ManagerAnalysisMode.period.value:
        try:
            requested_period = analysis_projection.ManagerAnalysisPeriod(period)
            period_projection = analysis_projection.build_projection(
                db,
                analysis_projection.ManagerAnalysisRequest(
                    scope=scope,
                    mode=analysis_projection.ManagerAnalysisMode.period,
                    period=requested_period,
                    custom_start=(
                        _custom_date(custom_start)
                        if requested_period
                        is analysis_projection.ManagerAnalysisPeriod.custom
                        else None
                    ),
                    custom_end=(
                        _custom_date(custom_end)
                        if requested_period
                        is analysis_projection.ManagerAnalysisPeriod.custom
                        else None
                    ),
                    channel_type=clean_channel_type,
                    status=clean_status,
                ),
            )
            period_facts = period_projection.facts
        except ValueError:
            # The POST returns the precise validation error; GET remains usable
            # while a manager is completing a custom range.
            period_facts = None
    return ManagerChatPageState(
        conversations=recent_conversation_options(
            db, scope=scope, selected_conversation_id=selected_id
        ),
        selected_conversation_id=selected_id,
        question=str(question or ""),
        answer=answer,
        error=redact_secret_text(error) if error else None,
        provider_enabled=ai_gateway.enabled(db),
        generation_enabled=control_registry.is_enabled(db, "ai.generation"),
        mode=mode,
        period=period,
        custom_start=custom_start or "",
        custom_end=custom_end or "",
        channel_type=clean_channel_type or "",
        status_filter=clean_status or "",
        channel_options=_channel_options(),
        status_options=_status_options(),
        period_facts=period_facts,
    )
