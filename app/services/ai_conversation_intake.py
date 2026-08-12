"""Durable conversational intake session helpers.

The AI intake owner records AI lifecycle, policy/version provenance and
generation evidence. Team Inbox remains the owner for conversation status,
routing, queueing, assignment and outbound delivery.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_intake import (
    AiIntakeConfig,
    AiIntakeGenerationAttempt,
    AiIntakePolicy,
    AiIntakePolicyVersion,
    AiIntakeSession,
)
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.schemas.ai_intake import AiIntakeOutcome, AiIntakeStatus
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

SUPPORTED_CONVERSATIONAL_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    }
)
TERMINAL_SESSION_STATES = frozenset(
    {
        "completed",
        "stopped_human_takeover",
        "fallback_escalated",
        "expired",
        "failed",
        "ineligible",
    }
)
DEFAULT_DISPLAY_NAME = "Dotmac Virtual Assistant"
DEFAULT_WELCOME_MESSAGE = (
    "Hello, I am Dotmac Virtual Assistant. I can help understand your request "
    "and connect you to the right team."
)
DEFAULT_QUEUE_POSITION_UPDATE_MINUTES = 5
DEFAULT_QUEUE_HEARTBEAT_MINUTES = 15
DEFAULT_QUEUE_TEMPLATES = {
    "initial": (
        "All our agents are currently engaged. You are number {position} in the "
        "queue. We will update you as your position changes."
    ),
    "position_update": "Quick update: you are now number {position} in the queue.",
    "heartbeat": (
        "You are still number {position} in the queue. We will connect you as "
        "soon as an agent is available."
    ),
    "handoff": "Thanks for waiting. An agent has joined and will continue from here.",
}
DEFAULT_DATA_CLEANUP_PROMPT = (
    "For NCC compliance, Dotmac needs your {fields}. You can share only the "
    "missing details, or tell us if you prefer not to disclose."
)
DEFAULT_DATA_CLEANUP_TEMPLATES = {
    "ask": DEFAULT_DATA_CLEANUP_PROMPT,
    "invalid_retry": (
        "I could not safely validate that detail. Please send only your "
        "{fields}, or tell us if you prefer not to disclose."
    ),
    "refused": ("No problem. I will leave that for the team to follow up if needed."),
    "saved": "Thank you. I have recorded the submitted profile details for review.",
    "follow_up": (
        "I will leave this for the team to follow up because I could not "
        "validate the requested details."
    ),
}
DEFAULT_GENDER_CHOICES = {
    "male": "male",
    "female": "female",
    "non_binary": "non_binary",
    "other": "other",
    "prefer_not_to_say": "unknown",
}
_AI_SESSION_COMMAND = OwnerCommandDefinition(
    owner="ai.intake",
    concern="AI conversational intake session lifecycle",
    name="execute_ai_intake_session_command",
)


@dataclass(frozen=True, slots=True)
class AiSessionProcessCommand:
    context: CommandContext
    limit: int = 100
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class AiSessionProcessResult:
    processed: int
    skipped: int
    failed: int


@dataclass(frozen=True, slots=True)
class AiSessionContext:
    session: AiIntakeSession
    policy: AiIntakePolicy | None
    version: AiIntakePolicyVersion | None


def is_supported_channel(channel_type: str | None) -> bool:
    return str(channel_type or "").strip() in SUPPORTED_CONVERSATIONAL_CHANNELS


def active_session_for_conversation(
    db: Session, conversation_id: UUID
) -> AiIntakeSession | None:
    return (
        db.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation_id)
        .filter(AiIntakeSession.completed_at.is_(None))
        .with_for_update()
        .one_or_none()
    )


def has_human_takeover(db: Session, conversation: InboxConversation) -> bool:
    if (
        not conversation.is_active
        or conversation.status == InboxConversationStatus.resolved.value
        or not is_supported_channel(conversation.channel_type)
    ):
        return True
    active_assignment = (
        db.query(InboxConversationAssignment.id)
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .first()
    )
    if active_assignment is not None:
        return True
    human_reply = (
        db.query(InboxMessage.id)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(
            func.coalesce(InboxMessage.metadata_["sent_by_person_id"].as_string(), "")
            != ""
        )
        .first()
    )
    return human_reply is not None


def ensure_policy_version_from_legacy_config(
    db: Session,
    *,
    config_id: UUID,
    provider: str,
    account_scope: str,
) -> tuple[AiIntakePolicy, AiIntakePolicyVersion]:
    config = db.get(AiIntakeConfig, config_id)
    if config is None:
        raise ValueError("AI intake config was not found")
    metadata = dict(config.metadata_ or {})
    display_name = str(metadata.get("display_name") or DEFAULT_DISPLAY_NAME).strip()
    if not display_name:
        display_name = DEFAULT_DISPLAY_NAME
    welcome_message = str(
        metadata.get("welcome_message") or DEFAULT_WELCOME_MESSAGE
    ).strip()
    if not welcome_message:
        welcome_message = DEFAULT_WELCOME_MESSAGE
    policy = (
        db.query(AiIntakePolicy)
        .filter(AiIntakePolicy.legacy_config_id == config.id)
        .filter(AiIntakePolicy.scope_key == config.scope_key)
        .filter(AiIntakePolicy.channel_type == config.channel_type)
        .filter(AiIntakePolicy.provider == "any")
        .filter(AiIntakePolicy.account_scope == "any")
        .one_or_none()
    )
    if policy is None:
        policy = AiIntakePolicy(
            legacy_config_id=config.id,
            scope_key=config.scope_key,
            channel_type=config.channel_type,
            provider="any",
            account_scope="any",
            display_name=display_name,
            is_enabled=config.is_enabled,
            fallback_team_id=config.fallback_team_id,
            metadata_={"compatibility_source": "ai_intake_configs"},
        )
        db.add(policy)
        db.flush()
    policy.display_name = display_name
    policy.is_enabled = config.is_enabled
    policy.fallback_team_id = config.fallback_team_id
    policy.metadata_ = {
        **dict(policy.metadata_ or {}),
        "compatibility_source": "ai_intake_configs",
        "max_turns": int(config.max_clarification_turns or 1) + 1,
        "confidence_threshold": float(config.confidence_threshold),
        "max_intake_duration_minutes": int(config.escalate_after_minutes or 5),
        "data_cleanup_enabled": bool(metadata.get("data_cleanup_enabled") or False),
    }

    last_version = (
        db.query(AiIntakePolicyVersion)
        .filter(AiIntakePolicyVersion.policy_id == policy.id)
        .order_by(AiIntakePolicyVersion.version_number.desc())
        .first()
    )
    queue_templates = metadata.get("queue_templates")
    if not isinstance(queue_templates, dict):
        queue_templates = {}
    queue_templates = {
        **DEFAULT_QUEUE_TEMPLATES,
        **{
            str(key): str(value)
            for key, value in queue_templates.items()
            if key in DEFAULT_QUEUE_TEMPLATES and str(value).strip()
        },
        "position_update_minutes": int(
            metadata.get("queue_position_update_minutes")
            or DEFAULT_QUEUE_POSITION_UPDATE_MINUTES
        ),
        "heartbeat_minutes": int(
            metadata.get("queue_heartbeat_minutes") or DEFAULT_QUEUE_HEARTBEAT_MINUTES
        ),
    }
    data_cleanup_policy = metadata.get("data_cleanup_policy")
    if not isinstance(data_cleanup_policy, dict):
        data_cleanup_policy = {}
    data_cleanup_policy = {
        **data_cleanup_policy,
        "prompt": str(
            data_cleanup_policy.get("prompt")
            or metadata.get("data_cleanup_prompt")
            or DEFAULT_DATA_CLEANUP_PROMPT
        ),
        "templates": {
            **DEFAULT_DATA_CLEANUP_TEMPLATES,
            **{
                str(key): str(value)
                for key, value in dict(
                    data_cleanup_policy.get("templates") or {}
                ).items()
                if key in DEFAULT_DATA_CLEANUP_TEMPLATES and str(value).strip()
            },
        },
        "max_attempts": 2,
        "gender_choices": data_cleanup_policy.get("gender_choices")
        or DEFAULT_GENDER_CHOICES,
        "dob_formats": data_cleanup_policy.get("dob_formats")
        or ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"),
        "production_collection_enabled": bool(
            metadata.get("data_cleanup_enabled") or False
        ),
    }
    version_signature = {
        "instructions": config.instructions or "",
        "welcome_message": welcome_message,
        "department_mappings": config.department_mappings or [],
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key
            not in {
                "preview_last_input",
                "preview_last_output",
                "last_prompt",
                "raw_prompt",
            }
        },
    }
    last_signature = (
        dict(last_version.metadata_ or {}).get("compatibility_signature")
        if last_version is not None
        else None
    )
    if last_version is None or last_signature != version_signature:
        version = AiIntakePolicyVersion(
            policy_id=policy.id,
            version_number=(last_version.version_number + 1 if last_version else 1),
            is_active=True,
            activated_at=datetime.now(UTC),
            display_name=display_name,
            welcome_message=welcome_message,
            business_tone=str(
                metadata.get("business_tone")
                or "Business casual, empathetic, smart and concise for a Nigerian ISP."
            ),
            business_instructions=config.instructions,
            approved_isp_information=str(metadata.get("approved_isp_information") or "")
            or None,
            intent_definitions=metadata.get("intent_definitions"),
            clarification_questions=metadata.get("clarification_questions"),
            intent_team_mappings=config.department_mappings,
            queue_templates=queue_templates,
            escalation_rules=metadata.get("escalation_rules"),
            data_cleanup_policy=data_cleanup_policy,
            metadata_={
                "compatibility_signature": version_signature,
                "provider_scope_observed": provider,
                "account_scope_observed": account_scope,
            },
        )
        if last_version is not None:
            last_version.is_active = False
        db.add(version)
        db.flush()
        policy.active_version_id = version.id
    else:
        version = last_version
        policy.active_version_id = version.id
    db.flush()
    return policy, version


def ensure_session_for_outcome(
    db: Session,
    *,
    conversation: InboxConversation,
    outcome: AiIntakeOutcome,
    provider: str,
    account_scope: str,
    created_conversation: bool,
) -> AiSessionContext | None:
    if not created_conversation or outcome.config_id is None:
        return None
    if outcome.status in {AiIntakeStatus.skipped, AiIntakeStatus.failed}:
        return None
    if not is_supported_channel(conversation.channel_type):
        return None
    if has_human_takeover(db, conversation):
        return None
    policy, version = ensure_policy_version_from_legacy_config(
        db,
        config_id=outcome.config_id,
        provider=provider,
        account_scope=account_scope,
    )
    session = active_session_for_conversation(db, conversation.id)
    now = datetime.now(UTC)
    if session is None:
        state = (
            "awaiting_customer"
            if outcome.status is AiIntakeStatus.awaiting_follow_up
            else "classified"
            if outcome.status is AiIntakeStatus.classified
            else "fallback_escalated"
            if outcome.status in {AiIntakeStatus.fallback, AiIntakeStatus.escalated}
            else "collecting_intent"
        )
        policy_metadata = dict(policy.metadata_ or {})
        session = AiIntakeSession(
            conversation_id=conversation.id,
            policy_id=policy.id,
            policy_version_id=version.id,
            legacy_config_id=outcome.config_id,
            state=state,
            channel_type=conversation.channel_type,
            provider=provider,
            account_scope=account_scope,
            display_name=version.display_name,
            turn_count=outcome.follow_up_count,
            max_turns=max(1, int(policy_metadata.get("max_turns") or 2)),
            confidence_threshold=outcome.classification.confidence
            if outcome.classification
            else float(policy_metadata.get("confidence_threshold") or 0),
            fallback_team_id=outcome.fallback_team_id or policy.fallback_team_id,
            expires_at=now
            + timedelta(
                minutes=int(
                    (policy.metadata_ or {}).get("max_intake_duration_minutes") or 10
                )
            ),
            metadata_={"ai_handling": True, "created_from": "team_inbox_receive"},
        )
        db.add(session)
    session.policy_id = policy.id
    session.policy_version_id = version.id
    session.legacy_config_id = outcome.config_id
    session.display_name = version.display_name
    session.turn_count = max(session.turn_count, outcome.follow_up_count)
    if outcome.classification is not None:
        session.final_intent = outcome.classification.intent.value
        session.final_category = outcome.classification.category.value
        session.final_confidence = outcome.classification.confidence
    db.flush()
    return AiSessionContext(session=session, policy=policy, version=version)


def ai_message_metadata(
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    purpose: str,
    turn_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_request_id: str | None = None,
) -> dict[str, object | None]:
    return {
        "sender_type": "ai",
        "author_type": "ai",
        "automation_kind": "ai_intake",
        "ai_display_name": session.display_name or DEFAULT_DISPLAY_NAME,
        "author_name": session.display_name or DEFAULT_DISPLAY_NAME,
        "ai_intake_session_id": str(session.id),
        "ai_intake_policy_id": str(session.policy_id) if session.policy_id else None,
        "ai_intake_policy_version_id": (
            str(session.policy_version_id) if session.policy_version_id else None
        ),
        "ai_intake_legacy_config_id": (
            str(session.legacy_config_id) if session.legacy_config_id else None
        ),
        "ai_message_purpose": purpose,
        "ai_turn_id": str(turn_id) if turn_id else None,
        "ai_provider": provider,
        "ai_model": model,
        "ai_provider_request_id": provider_request_id,
        "ai_generated_at": datetime.now(UTC).isoformat(),
        "ai_human_takeover_status": session.state,
        "ai_policy_version_number": version.version_number if version else None,
    }


def record_generation_attempt(
    db: Session,
    *,
    session: AiIntakeSession,
    purpose: str,
    status: str,
    inbound_message_id: UUID | None = None,
    outbound_message_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> AiIntakeGenerationAttempt:
    turn_number = max(1, session.turn_count or 1)
    dedupe = (
        f"ai-intake-generation:{session.id}:"
        f"{inbound_message_id or 'system'}:{purpose}:{turn_number}"
    )
    existing = (
        db.query(AiIntakeGenerationAttempt)
        .filter(AiIntakeGenerationAttempt.idempotency_key == dedupe)
        .one_or_none()
    )
    if existing is not None:
        if outbound_message_id and existing.outbound_message_id is None:
            existing.outbound_message_id = outbound_message_id
        return existing
    attempt = AiIntakeGenerationAttempt(
        session_id=session.id,
        inbound_message_id=inbound_message_id,
        outbound_message_id=outbound_message_id,
        turn_number=turn_number,
        message_purpose=purpose,
        provider=provider,
        model=model,
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
        idempotency_key=dedupe,
        generated_at=datetime.now(UTC) if status == "queued" else None,
    )
    db.add(attempt)
    db.flush()
    return attempt


def mark_handoff_requested(
    session: AiIntakeSession,
    *,
    destination_team_id: str | UUID | None,
) -> None:
    session.state = "handoff_requested"
    session.handoff_requested_at = datetime.now(UTC)
    metadata = dict(session.metadata_ or {})
    metadata["destination_team_id"] = (
        str(destination_team_id) if destination_team_id else None
    )
    session.metadata_ = metadata


def complete_session(session: AiIntakeSession, *, state: str = "completed") -> None:
    if state not in TERMINAL_SESSION_STATES:
        raise ValueError("AI intake terminal state is invalid")
    session.state = state
    now = datetime.now(UTC)
    session.completed_at = now
    if state == "stopped_human_takeover":
        session.takeover_at = now
    metadata = dict(session.metadata_ or {})
    metadata["ai_handling"] = False
    session.metadata_ = metadata


def mark_conversation_ai_metadata(
    conversation: InboxConversation,
    *,
    session: AiIntakeSession | None,
    active: bool,
) -> None:
    metadata = dict(conversation.metadata_ or {})
    metadata["ai_handling"] = bool(active)
    if session is not None:
        metadata["ai_intake_session_id"] = str(session.id)
        metadata["ai_intake_state"] = session.state
    metadata["ai_intake_display_name"] = session.display_name
    conversation.metadata_ = metadata


def process_ready_sessions(
    db: Session, command: AiSessionProcessCommand
) -> AiSessionProcessResult:
    """Classify pending sessions and request Team Inbox consequences."""

    def _operation() -> AiSessionProcessResult:
        processed = 0
        skipped = 0
        failed = 0
        rows = (
            db.query(AiIntakeSession)
            .filter(AiIntakeSession.completed_at.is_(None))
            .filter(
                AiIntakeSession.state.in_(
                    (
                        "collecting_intent",
                        "awaiting_customer",
                        "eligible",
                        "welcome_pending",
                        "handoff_requested",
                    )
                )
            )
            .order_by(AiIntakeSession.created_at.asc())
            .limit(command.limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for session in rows:
            conversation = db.get(InboxConversation, session.conversation_id)
            if conversation is None or has_human_takeover(db, conversation):
                complete_session(session, state="stopped_human_takeover")
                if conversation is not None:
                    mark_conversation_ai_metadata(
                        conversation, session=session, active=False
                    )
                skipped += 1
                continue
            try:
                if _process_one_session(db, session=session, conversation=conversation):
                    processed += 1
                else:
                    skipped += 1
            except Exception:
                session.state = "failed"
                session.completed_at = datetime.now(UTC)
                failed += 1
        return AiSessionProcessResult(
            processed=processed, skipped=skipped, failed=failed
        )

    return execute_owner_command(
        db,
        definition=_AI_SESSION_COMMAND,
        context=command.context,
        operation=_operation,
    )


def _process_one_session(
    db: Session, *, session: AiIntakeSession, conversation: InboxConversation
) -> bool:
    from app.schemas.ai_intake import AiIntakeContextMessage, AiIntakeRequest
    from app.services import (
        ai_intake,
        team_inbox_assignment,
        team_inbox_outbound,
        team_inbox_routing,
    )

    inbound = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    if inbound is None or not inbound.body:
        return False
    version = (
        db.get(AiIntakePolicyVersion, session.policy_version_id)
        if session.policy_version_id
        else None
    )
    cleanup_only = session.state == "handoff_requested"
    cleanup_open = _process_data_cleanup_turn(
        db,
        session=session,
        version=version,
        conversation=conversation,
        inbound=inbound,
    )
    if cleanup_only:
        if not cleanup_open:
            complete_session(session)
            mark_conversation_ai_metadata(conversation, session=session, active=False)
        return True
    metadata = dict(inbound.metadata_ or {})
    processed_key = f"processed_inbound:{inbound.id}"
    if dict(session.metadata_ or {}).get(processed_key):
        return False
    recent_rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .order_by(InboxMessage.created_at.desc())
        .limit(ai_intake.MAX_RECENT_MESSAGES)
        .all()
    )
    recent = tuple(
        AiIntakeContextMessage(
            direction=(
                "inbound"
                if row.direction == InboxMessageDirection.inbound.value
                else "outbound"
            ),
            body=str(row.body or "")[: ai_intake.MAX_CONTEXT_CHARS],
        )
        for row in reversed(recent_rows)
        if str(row.body or "").strip()
    )
    request = AiIntakeRequest(
        channel_type=conversation.channel_type,
        provider=session.provider,
        account_scope=session.account_scope,
        inbound_message_id=str(inbound.external_message_id or inbound.id)[:255],
        body=str(inbound.body or "")[:4000],
        conversation_id=conversation.id,
        recent_messages=recent,
        campaign_attributed=False,
        routing_allows_ai=True,
        created_conversation=True,
        has_active_assignment=False,
        awaiting_follow_up=session.state == "awaiting_customer",
        follow_up_count=session.turn_count,
    )
    outcome = ai_intake.classify_message(db, request)
    metadata.update(ai_intake.route_metadata(outcome))
    inbound.metadata_ = metadata
    session.turn_count = outcome.follow_up_count
    if outcome.classification is not None:
        session.final_intent = outcome.classification.intent.value
        session.final_category = outcome.classification.category.value
        session.final_confidence = outcome.classification.confidence
    generation = record_generation_attempt(
        db,
        session=session,
        purpose="classification",
        status=outcome.status.value,
        inbound_message_id=inbound.id,
        provider=outcome.provider,
        model=outcome.model,
        duration_ms=outcome.duration_ms,
        error_code=outcome.reason.value,
    )
    session_metadata = dict(session.metadata_ or {})
    session_metadata[processed_key] = True
    session_metadata["last_generation_attempt_id"] = str(generation.id)
    session.metadata_ = session_metadata
    if has_human_takeover(db, conversation):
        complete_session(session, state="stopped_human_takeover")
        mark_conversation_ai_metadata(conversation, session=session, active=False)
        return True
    if (
        outcome.status is AiIntakeStatus.awaiting_follow_up
        and outcome.config_id is not None
        and outcome.classification is not None
        and outcome.classification.follow_up_question is not None
    ):
        delivery = team_inbox_outbound.send_ai_intake_follow_up(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.AiIntakeFollowUpPayload(
                question=outcome.classification.follow_up_question,
                inbound_message_id=inbound.id,
                config_id=outcome.config_id,
                follow_up_count=outcome.follow_up_count,
                session_id=session.id,
                policy_id=session.policy_id,
                policy_version_id=session.policy_version_id,
                display_name=session.display_name,
            ),
        )
        generation.outbound_message_id = (
            UUID(delivery.message_id) if delivery.message_id else None
        )
        session.state = "awaiting_customer"
        conversation.status = InboxConversationStatus.pending.value
        mark_conversation_ai_metadata(conversation, session=session, active=True)
        return True
    routing = team_inbox_routing.resolve_channel_routing_decision(
        db,
        channel_type=conversation.channel_type,
        provider=session.provider,
        account_scope=session.account_scope,
        fallback_service_team_id=session.fallback_team_id,
        metadata=metadata,
    )
    if routing.primary_service_team_id:
        team_inbox_routing.apply_email_routing_plan(
            db,
            conversation=conversation,
            plan=team_inbox_routing.EmailTeamRoutingPlan(
                primary_service_team_id=routing.primary_service_team_id,
                participant_service_team_ids=[routing.primary_service_team_id],
                matches=[],
                unmatched_recipients=[],
            ),
        )
        mark_handoff_requested(
            session, destination_team_id=routing.primary_service_team_id
        )
        conversation.status = InboxConversationStatus.open.value
        assignment = team_inbox_assignment.assign_conversation_to_available_agent(
            db,
            conversation=conversation,
            service_team_id=routing.primary_service_team_id,
            reason="AI intake handoff",
            source="routing_rule",
        )
        if assignment.kind == "assigned" or not cleanup_open:
            complete_session(session)
            mark_conversation_ai_metadata(conversation, session=session, active=False)
        else:
            mark_conversation_ai_metadata(conversation, session=session, active=True)
    else:
        session.state = "fallback_escalated"
        complete_session(session, state="fallback_escalated")
        mark_conversation_ai_metadata(conversation, session=session, active=False)
    return True


def _data_cleanup_policy(version: AiIntakePolicyVersion | None) -> dict[str, object]:
    raw = dict(version.data_cleanup_policy or {}) if version is not None else {}
    templates = raw.get("templates")
    if not isinstance(templates, dict):
        templates = {}
    return {
        **raw,
        "templates": {
            **DEFAULT_DATA_CLEANUP_TEMPLATES,
            **{
                str(key): str(value)
                for key, value in templates.items()
                if key in DEFAULT_DATA_CLEANUP_TEMPLATES and str(value).strip()
            },
        },
        "gender_choices": raw.get("gender_choices") or DEFAULT_GENDER_CHOICES,
        "dob_formats": raw.get("dob_formats") or ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"),
        "max_attempts": min(max(int(raw.get("max_attempts") or 2), 1), 2),
        "production_collection_enabled": bool(raw.get("production_collection_enabled")),
    }


def _format_cleanup_fields(fields: tuple[str, ...]) -> str:
    labels = {
        "gender": "gender",
        "date_of_birth": "date of birth",
    }
    return " and ".join(labels.get(field, field) for field in fields)


def _cleanup_state(session: AiIntakeSession) -> dict[str, object]:
    metadata = dict(session.metadata_ or {})
    state = metadata.get("data_cleanup")
    return dict(state) if isinstance(state, dict) else {}


def _store_cleanup_state(session: AiIntakeSession, state: Mapping[str, object]) -> None:
    metadata = dict(session.metadata_ or {})
    metadata["data_cleanup"] = dict(state)
    session.metadata_ = metadata


def _send_cleanup_message(
    db: Session,
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    conversation: InboxConversation,
    body: str,
    purpose: str,
    dedupe_suffix: str,
) -> None:
    from app.services import team_inbox_outbound

    team_inbox_outbound.send_ai_intake_message(
        db,
        conversation=conversation,
        body_text=body,
        metadata=ai_message_metadata(
            session=session,
            version=version,
            purpose=purpose,
        ),
        dedupe_key=f"ai-intake-cleanup:{session.id}:{dedupe_suffix}",
    )


def _parse_cleanup_response(
    body: str,
    *,
    missing_fields: tuple[str, ...],
    policy: Mapping[str, object],
) -> tuple[str, str | None, date | None]:
    text = body.strip()
    normalized = text.lower()
    if re.search(r"\b(no|decline|refuse|prefer not|not disclose|skip)\b", normalized):
        return "refused", None, None
    gender: str | None = None
    gender_choices = policy.get("gender_choices")
    choices = gender_choices if isinstance(gender_choices, Mapping) else {}
    for public_value in choices:
        token = str(public_value).strip().lower().replace("_", " ")
        if token and re.search(rf"\b{re.escape(token)}\b", normalized):
            gender = str(public_value)
            break
    dob: date | None = None
    if "date_of_birth" in missing_fields:
        candidates = re.findall(
            r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}/\d{2}/\d{4}\b|\b\d{2}-\d{2}-\d{4}\b", text
        )
        formats = tuple(str(item) for item in policy.get("dob_formats") or ())
        for candidate in candidates:
            for fmt in formats:
                try:
                    parsed = datetime.strptime(candidate, fmt).date()
                except ValueError:
                    continue
                if parsed < date.today():
                    dob = parsed
                    break
            if dob is not None:
                break
    has_gender = "gender" not in missing_fields or gender is not None
    has_dob = "date_of_birth" not in missing_fields or dob is not None
    if has_gender and has_dob:
        return "valid", gender, dob
    return "invalid", gender, dob


def _process_data_cleanup_turn(
    db: Session,
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    conversation: InboxConversation,
    inbound: InboxMessage,
) -> bool:
    from app.models.subscriber import Subscriber
    from app.services import subscriber_profile_cleanup

    policy = _data_cleanup_policy(version)
    if not policy.get("production_collection_enabled"):
        return False
    if conversation.subscriber_id is None:
        return False
    subscriber = db.get(Subscriber, conversation.subscriber_id)
    if (
        subscriber is None
        or not subscriber_profile_cleanup.is_direct_residential_customer(subscriber)
    ):
        return False
    missing = subscriber_profile_cleanup.missing_cleanup_fields(subscriber)
    if not missing:
        return False
    state = _cleanup_state(session)
    processed = state.get("processed_inbound_message_ids")
    processed_ids = list(processed) if isinstance(processed, list) else []
    if str(inbound.id) in processed_ids:
        return state.get("status") == "awaiting_response"
    attempts = int(state.get("attempts") or 0)
    templates = policy["templates"]
    assert isinstance(templates, Mapping)
    field_text = _format_cleanup_fields(missing)
    if state.get("status") != "awaiting_response":
        attempts += 1
        body = str(templates["ask"]).format(fields=field_text)
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=body,
            purpose="data_cleanup",
            dedupe_suffix=f"ask:{attempts}",
        )
        _store_cleanup_state(
            session,
            {
                "status": "awaiting_response",
                "attempts": attempts,
                "missing_fields": list(missing),
                "processed_inbound_message_ids": processed_ids,
            },
        )
        return True
    outcome, gender, dob = _parse_cleanup_response(
        str(inbound.body or ""),
        missing_fields=missing,
        policy=policy,
    )
    processed_ids.append(str(inbound.id))
    if outcome == "valid":
        result = subscriber_profile_cleanup.submit_profile_cleanup(
            db,
            subscriber_profile_cleanup.SubmitProfileCleanupCommand(
                context=CommandContext.system(
                    actor="ai:dotmac-virtual-assistant",
                    scope="customer:profile-cleanup",
                    reason="save AI-collected NCC profile cleanup candidates",
                ),
                subscriber_id=subscriber.id,
                source_conversation_id=conversation.id,
                candidate_gender=gender,
                candidate_date_of_birth=dob,
                gender_mapping=policy.get("gender_choices")
                if isinstance(policy.get("gender_choices"), Mapping)
                else None,
                consent_text="customer supplied profile cleanup candidate",
                attempt_count=attempts,
                activation_enabled=bool(policy.get("production_collection_enabled")),
            ),
        )
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=str(templates["saved"]),
            purpose="data_cleanup",
            dedupe_suffix="saved",
        )
        _store_cleanup_state(
            session,
            {
                "status": result.outcome.value,
                "attempts": attempts,
                "missing_fields": list(missing),
                "saved_fields": list(result.saved_fields),
                "processed_inbound_message_ids": processed_ids[-10:],
            },
        )
        return False
    if outcome == "refused":
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=str(templates["refused"]),
            purpose="data_cleanup",
            dedupe_suffix="refused",
        )
        _store_cleanup_state(
            session,
            {
                "status": "refused",
                "attempts": attempts,
                "missing_fields": list(missing),
                "processed_inbound_message_ids": processed_ids[-10:],
            },
        )
        return False
    if attempts >= int(policy["max_attempts"]):
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=str(templates["follow_up"]),
            purpose="data_cleanup",
            dedupe_suffix="human-follow-up",
        )
        _store_cleanup_state(
            session,
            {
                "status": "human_follow_up_required",
                "attempts": attempts,
                "missing_fields": list(missing),
                "processed_inbound_message_ids": processed_ids[-10:],
            },
        )
        return False
    attempts += 1
    _send_cleanup_message(
        db,
        session=session,
        version=version,
        conversation=conversation,
        body=str(templates["invalid_retry"]).format(fields=field_text),
        purpose="data_cleanup",
        dedupe_suffix=f"invalid:{attempts}",
    )
    _store_cleanup_state(
        session,
        {
            "status": "awaiting_response",
            "attempts": attempts,
            "missing_fields": list(missing),
            "processed_inbound_message_ids": processed_ids[-10:],
        },
    )
    return True
