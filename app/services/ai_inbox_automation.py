"""AI intake policy, context, and classification for Team Inbox automation.

This module owns the AI decision inputs for customer-facing inbox automation.
It does not send replies, assign conversations, mutate support tickets, or
update customer state. Team Inbox owns those side effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakeConfig
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation
from app.services import control_registry
from app.services.ai.client import AIClientError
from app.services.ai.gateway import ai_gateway
from app.services.ai.output_parsers import parse_json_object, require_keys
from app.services.ai.security import ai_enabled
from app.services.portal_account_health import (
    PortalAccountHealth,
    build_portal_account_health,
)
from app.services.topology.customer_path import resolve_customer_path

DEFAULT_SCOPE_KEY = "inbox:default"
OWNER = "ai.intake"


class IntakeChannel(StrEnum):
    email = "email"
    whatsapp = "whatsapp"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"
    chat_widget = "chat_widget"


class ContextSourceKey(StrEnum):
    contact_identity = "contact_identity"
    account_health = "account_health"
    prepaid_balance = "prepaid_balance"
    profile_completion = "profile_completion"
    service_area_health = "service_area_health"
    access_path = "access_path"


class WorkflowAction(StrEnum):
    classify_intent = "classify_intent"
    ask_clarifying_question = "ask_clarifying_question"
    validate_customer = "validate_customer"
    read_account_context = "read_account_context"
    propose_reply = "propose_reply"
    route_to_team = "route_to_team"
    handoff_to_live_agent = "handoff_to_live_agent"
    end_conversation = "end_conversation"


class HandoffPolicy(StrEnum):
    manual_review = "manual_review"
    live_agent = "live_agent"
    close_only = "close_only"


class AssignmentStrategy(StrEnum):
    available_round_robin = "available_round_robin"
    queue_only = "queue_only"


@dataclass(frozen=True, slots=True)
class ContextSourceOption:
    key: ContextSourceKey
    label: str
    owner: str
    description: str


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    position: int
    action: WorkflowAction
    prompt: str
    required_context: tuple[ContextSourceKey, ...]
    handoff_on_failure: bool


@dataclass(frozen=True, slots=True)
class DepartmentMapping:
    intent: str
    service_team_id: UUID | None
    label: str


@dataclass(frozen=True, slots=True)
class IntakePolicy:
    scope_key: str
    channel_type: IntakeChannel
    is_enabled: bool
    auto_reply_enabled: bool
    auto_handoff_enabled: bool
    confidence_threshold: float
    allow_followup_questions: bool
    max_clarification_turns: int
    escalate_after_minutes: int
    fallback_team_id: UUID | None
    handoff_policy: HandoffPolicy
    assignment_strategy: AssignmentStrategy
    instructions: str | None
    department_mappings: tuple[DepartmentMapping, ...]
    workflow_steps: tuple[WorkflowStep, ...]
    context_sources: tuple[ContextSourceKey, ...]


@dataclass(frozen=True, slots=True)
class EffectiveAutomationState:
    scope_key: str
    configured: bool
    provider_enabled: bool
    generation_control_enabled: bool
    intake_enabled: bool
    auto_reply_enabled: bool
    auto_handoff_enabled: bool
    may_classify: bool
    may_send_customer_reply: bool
    may_handoff: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ConversationAutomationContext:
    conversation_id: UUID
    subscriber_id: UUID | None
    contact_address: str | None
    channel_type: str
    account_health: PortalAccountHealth | None
    profile_missing_fields: tuple[str, ...]
    access_paths: tuple[dict[str, Any], ...]
    unavailable_sources: tuple[ContextSourceKey, ...]


@dataclass(frozen=True, slots=True)
class IntakeDecision:
    intent: str
    confidence: float
    has_enough_information: bool
    should_handoff: bool
    target_service_team_id: UUID | None
    followup_question: str | None
    customer_reply: str | None
    should_close: bool
    rationale: str | None
    raw: dict[str, Any]


CONTEXT_SOURCE_OPTIONS: tuple[ContextSourceOption, ...] = (
    ContextSourceOption(
        key=ContextSourceKey.contact_identity,
        label="Contact identity",
        owner="communications.team_inbox_contact_resolution",
        description="Reviewed subscriber/reseller link and ambiguity state.",
    ),
    ContextSourceOption(
        key=ContextSourceKey.account_health,
        label="Portal service health",
        owner="portal.account_health",
        description="The same lifecycle, access, session and outage projection used by the portal card.",
    ),
    ContextSourceOption(
        key=ContextSourceKey.prepaid_balance,
        label="Prepaid balance",
        owner="financial.customer_position",
        description="Customer-safe funding state from the account-health projection.",
    ),
    ContextSourceOption(
        key=ContextSourceKey.profile_completion,
        label="Profile completion",
        owner="customer.identity_scope",
        description="Missing customer fields the assistant may ask the customer to provide.",
    ),
    ContextSourceOption(
        key=ContextSourceKey.service_area_health,
        label="Known area issue",
        owner="network.outage_projection",
        description="Area-outage signal already carried by portal service health.",
    ),
    ContextSourceOption(
        key=ContextSourceKey.access_path,
        label="Base station and access path",
        owner="network.access_path",
        description="Subscriber service path context, when exposed through approved read models.",
    ),
)

DEFAULT_WORKFLOW: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        position=1,
        action=WorkflowAction.classify_intent,
        prompt="Classify the customer intent using only the conversation and enabled context.",
        required_context=(ContextSourceKey.contact_identity,),
        handoff_on_failure=True,
    ),
    WorkflowStep(
        position=2,
        action=WorkflowAction.validate_customer,
        prompt="If identity is not already verified, ask for account-safe validation details.",
        required_context=(ContextSourceKey.contact_identity,),
        handoff_on_failure=True,
    ),
    WorkflowStep(
        position=3,
        action=WorkflowAction.read_account_context,
        prompt="Read account health, prepaid funding, profile gaps, and known service issues.",
        required_context=(
            ContextSourceKey.account_health,
            ContextSourceKey.prepaid_balance,
            ContextSourceKey.profile_completion,
            ContextSourceKey.service_area_health,
        ),
        handoff_on_failure=True,
    ),
    WorkflowStep(
        position=4,
        action=WorkflowAction.route_to_team,
        prompt="Map the classified intent to the configured service team or fallback team.",
        required_context=(ContextSourceKey.contact_identity,),
        handoff_on_failure=True,
    ),
)


def context_source_options() -> tuple[ContextSourceOption, ...]:
    return CONTEXT_SOURCE_OPTIONS


def default_workflow_steps() -> tuple[WorkflowStep, ...]:
    return DEFAULT_WORKFLOW


def _coerce_channel(value: str | None) -> IntakeChannel:
    try:
        return IntakeChannel(str(value or IntakeChannel.chat_widget.value))
    except ValueError:
        return IntakeChannel.chat_widget


def _coerce_handoff(value: str | None) -> HandoffPolicy:
    try:
        return HandoffPolicy(str(value or HandoffPolicy.manual_review.value))
    except ValueError:
        return HandoffPolicy.manual_review


def _coerce_assignment_strategy(value: str | None) -> AssignmentStrategy:
    try:
        return AssignmentStrategy(
            str(value or AssignmentStrategy.available_round_robin.value)
        )
    except ValueError:
        return AssignmentStrategy.available_round_robin


def _coerce_context_source(value: object) -> ContextSourceKey | None:
    try:
        return ContextSourceKey(str(value))
    except ValueError:
        return None


def _uuid_or_none(value: object) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _department_mappings(raw: object) -> tuple[DepartmentMapping, ...]:
    if not isinstance(raw, list):
        return ()
    rows: list[DepartmentMapping] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent") or item.get("keyword") or "").strip()
        if not intent:
            continue
        rows.append(
            DepartmentMapping(
                intent=intent,
                service_team_id=_uuid_or_none(
                    item.get("service_team_id") or item.get("team_id")
                ),
                label=str(item.get("label") or intent).strip() or intent,
            )
        )
    return tuple(rows)


def _workflow_steps(raw: object) -> tuple[WorkflowStep, ...]:
    if not isinstance(raw, list):
        return DEFAULT_WORKFLOW
    rows: list[WorkflowStep] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        try:
            action = WorkflowAction(str(item.get("action") or ""))
        except ValueError:
            continue
        sources = tuple(
            source
            for source in (
                _coerce_context_source(value)
                for value in item.get("required_context", [])
                if isinstance(item.get("required_context"), list)
            )
            if source is not None
        )
        rows.append(
            WorkflowStep(
                position=int(item.get("position") or index),
                action=action,
                prompt=str(item.get("prompt") or "").strip(),
                required_context=sources,
                handoff_on_failure=item.get("handoff_on_failure") is not False,
            )
        )
    return tuple(sorted(rows, key=lambda row: row.position)) or DEFAULT_WORKFLOW


def _context_sources(raw: object) -> tuple[ContextSourceKey, ...]:
    if not isinstance(raw, list):
        return (
            ContextSourceKey.contact_identity,
            ContextSourceKey.account_health,
            ContextSourceKey.prepaid_balance,
            ContextSourceKey.profile_completion,
            ContextSourceKey.service_area_health,
        )
    seen: set[ContextSourceKey] = set()
    rows: list[ContextSourceKey] = []
    for item in raw:
        source = _coerce_context_source(item)
        if source is not None and source not in seen:
            seen.add(source)
            rows.append(source)
    return tuple(rows)


def policy_from_config(config: AiIntakeConfig | None) -> IntakePolicy:
    if config is None:
        return IntakePolicy(
            scope_key=DEFAULT_SCOPE_KEY,
            channel_type=IntakeChannel.chat_widget,
            is_enabled=False,
            auto_reply_enabled=False,
            auto_handoff_enabled=False,
            confidence_threshold=0.75,
            allow_followup_questions=True,
            max_clarification_turns=1,
            escalate_after_minutes=5,
            fallback_team_id=None,
            handoff_policy=HandoffPolicy.manual_review,
            assignment_strategy=AssignmentStrategy.available_round_robin,
            instructions=None,
            department_mappings=(),
            workflow_steps=DEFAULT_WORKFLOW,
            context_sources=_context_sources(None),
        )
    return IntakePolicy(
        scope_key=config.scope_key,
        channel_type=_coerce_channel(config.channel_type),
        is_enabled=config.is_enabled,
        auto_reply_enabled=config.auto_reply_enabled,
        auto_handoff_enabled=config.auto_handoff_enabled,
        confidence_threshold=float(config.confidence_threshold),
        allow_followup_questions=config.allow_followup_questions,
        max_clarification_turns=config.max_clarification_turns,
        escalate_after_minutes=config.escalate_after_minutes,
        fallback_team_id=config.fallback_team_id,
        handoff_policy=_coerce_handoff(config.handoff_policy),
        assignment_strategy=_coerce_assignment_strategy(config.assignment_strategy),
        instructions=config.instructions,
        department_mappings=_department_mappings(config.department_mappings),
        workflow_steps=_workflow_steps(config.workflow_steps),
        context_sources=_context_sources(config.context_sources),
    )


def get_config(
    db: Session, *, scope_key: str = DEFAULT_SCOPE_KEY
) -> AiIntakeConfig | None:
    return (
        db.query(AiIntakeConfig)
        .filter(AiIntakeConfig.scope_key == scope_key)
        .one_or_none()
    )


def effective_state(
    db: Session, *, scope_key: str = DEFAULT_SCOPE_KEY
) -> EffectiveAutomationState:
    config = get_config(db, scope_key=scope_key)
    policy = policy_from_config(config)
    provider_on = ai_enabled(db)
    generation_on = control_registry.is_enabled(db, "ai.generation")
    may_classify = bool(
        config is not None and policy.is_enabled and provider_on and generation_on
    )
    may_reply = bool(may_classify and policy.auto_reply_enabled)
    may_handoff = bool(may_classify and policy.auto_handoff_enabled)
    if config is None:
        reason = "No intake policy has been configured for this scope."
    elif not provider_on:
        reason = "The AI provider setting is disabled."
    elif not generation_on:
        reason = "The ai.generation control is disabled."
    elif not policy.is_enabled:
        reason = "The inbox intake policy is disabled."
    elif not policy.auto_reply_enabled and not policy.auto_handoff_enabled:
        reason = (
            "Classification may run, but customer replies and handoff remain disabled."
        )
    else:
        reason = "Classification is enabled with configured customer actions."
    return EffectiveAutomationState(
        scope_key=scope_key,
        configured=config is not None,
        provider_enabled=provider_on,
        generation_control_enabled=generation_on,
        intake_enabled=policy.is_enabled,
        auto_reply_enabled=policy.auto_reply_enabled,
        auto_handoff_enabled=policy.auto_handoff_enabled,
        may_classify=may_classify,
        may_send_customer_reply=may_reply,
        may_handoff=may_handoff,
        reason=reason,
    )


def _profile_missing_fields(subscriber: Subscriber | None) -> tuple[str, ...]:
    if subscriber is None:
        return ()
    checks = {
        "date_of_birth": subscriber.date_of_birth,
        "phone": subscriber.phone,
        "address_line1": subscriber.address_line1,
        "city": subscriber.city,
        "region": subscriber.region,
        "lga": subscriber.lga,
    }
    return tuple(field for field, value in checks.items() if not value)


def _access_path_payload(
    db: Session, subscriber_id: UUID
) -> tuple[dict[str, Any], ...]:
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .order_by(Subscription.created_at.desc())
        .limit(5)
        .all()
    )
    paths: list[dict[str, Any]] = []
    for subscription in subscriptions:
        try:
            path = resolve_customer_path(db, subscription)
        except Exception:
            paths.append(
                {
                    "subscription_id": str(subscription.id),
                    "gap": "unavailable",
                }
            )
            continue
        paths.append(
            {
                "subscription_id": str(subscription.id),
                "access_device_kind": path.access_device_kind,
                "access_device_name": getattr(path.access_device, "name", None)
                or getattr(path.access_device, "hostname", None),
                "basestation_id": str(path.basestation.id)
                if path.basestation is not None
                else None,
                "basestation_name": getattr(path.basestation, "name", None),
                "live_session": path.live_session,
                "gap": path.gap,
            }
        )
    return tuple(paths)


def conversation_context(
    db: Session, *, conversation_id: UUID, policy: IntakePolicy
) -> ConversationAutomationContext:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        raise ValueError("Conversation was not found.")
    account_health: PortalAccountHealth | None = None
    profile_missing_fields: tuple[str, ...] = ()
    access_paths: tuple[dict[str, Any], ...] = ()
    unavailable: list[ContextSourceKey] = []
    subscriber = (
        db.get(Subscriber, conversation.subscriber_id)
        if conversation.subscriber_id is not None
        else None
    )
    if (
        ContextSourceKey.profile_completion in policy.context_sources
        and conversation.subscriber_id is not None
    ):
        profile_missing_fields = _profile_missing_fields(subscriber)
    elif ContextSourceKey.profile_completion in policy.context_sources:
        unavailable.append(ContextSourceKey.profile_completion)
    if (
        ContextSourceKey.account_health in policy.context_sources
        and conversation.subscriber_id is not None
    ):
        try:
            account_health = build_portal_account_health(db, conversation.subscriber_id)
        except Exception:
            unavailable.append(ContextSourceKey.account_health)
    elif ContextSourceKey.account_health in policy.context_sources:
        unavailable.append(ContextSourceKey.account_health)
    if (
        ContextSourceKey.access_path in policy.context_sources
        and conversation.subscriber_id is not None
    ):
        access_paths = _access_path_payload(db, conversation.subscriber_id)
    elif ContextSourceKey.access_path in policy.context_sources:
        unavailable.append(ContextSourceKey.access_path)
    return ConversationAutomationContext(
        conversation_id=conversation.id,
        subscriber_id=conversation.subscriber_id,
        contact_address=conversation.contact_address,
        channel_type=conversation.channel_type,
        account_health=account_health,
        profile_missing_fields=profile_missing_fields,
        access_paths=access_paths,
        unavailable_sources=tuple(unavailable),
    )


def _state_value_payload(value: object) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    kind = getattr(value, "kind", None)
    if kind is not None:
        payload["kind"] = getattr(kind, "value", str(kind))
    if hasattr(value, "value"):
        raw_value = value.value
        if isinstance(raw_value, Decimal):
            payload["value"] = decimal_to_text(raw_value)
        elif isinstance(raw_value, UUID):
            payload["value"] = str(raw_value)
        elif isinstance(raw_value, str | int | float | bool) or raw_value is None:
            payload["value"] = raw_value
        else:
            payload["value"] = str(raw_value)
    reason = getattr(value, "reason", None)
    if reason:
        payload["reason"] = str(reason)
    return payload


def _status_payload(value: object) -> dict[str, Any]:
    return {
        key: item
        for key, item in {
            "state": getattr(value, "state", None),
            "label": getattr(value, "label", None),
            "tone": getattr(value, "tone", None),
            "message": getattr(value, "message", None),
        }.items()
        if item is not None
    }


def _datetime_text(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _account_health_payload(
    health: PortalAccountHealth | None,
) -> dict[str, Any] | None:
    if health is None:
        return None
    return {
        "account_id": str(health.account_id),
        "account_number": health.account_number,
        "subscriber_number": health.subscriber_number,
        "display_name": health.display_name,
        "lifecycle": _status_payload(health.lifecycle),
        "billing_mode": _state_value_payload(health.financial.billing_mode),
        "receivables": _state_value_payload(health.financial.receivables),
        "prepaid_funding": _state_value_payload(health.financial.prepaid_funding),
        "prepaid_funding_reason": health.financial.prepaid_funding_reason,
        "has_partial_data": health.has_partial_data,
        "as_of": health.as_of.isoformat(),
        "services": [
            {
                "subscription_id": str(service.subscription_id),
                "offer_name": service.offer_name,
                "lifecycle": _status_payload(service.lifecycle),
                "billing_mode": service.billing_mode,
                "access_state": service.access_state.value,
                "access": _status_payload(service.access),
                "access_reason": service.access_reason,
                "session_state": getattr(service.session, "state", None),
                "connection": _state_value_payload(service.connection),
                "known_area_outage": bool(
                    getattr(
                        getattr(service.connection, "value", None), "area_outage", False
                    )
                ),
                "next_charge_at": _datetime_text(service.next_charge_at),
                "expires_at": _datetime_text(service.expires_at),
                "next_action": service.next_action.value
                if service.next_action is not None
                else None,
                "customer_action_url": service.customer_action_url,
            }
            for service in health.services
        ],
    }


def _context_payload(
    *, context: ConversationAutomationContext, inbound_body: str, policy: IntakePolicy
) -> dict[str, Any]:
    enabled_sources = {source.value for source in policy.context_sources}
    payload: dict[str, Any] = {
        "latest_customer_message": inbound_body[:4000],
        "conversation": {
            "id": str(context.conversation_id),
            "channel_type": context.channel_type,
            "contact_address_present": bool(context.contact_address),
            "subscriber_id": str(context.subscriber_id)
            if context.subscriber_id is not None
            else None,
        },
        "enabled_context_sources": sorted(enabled_sources),
        "unavailable_sources": [source.value for source in context.unavailable_sources],
        "department_mappings": [
            {
                "intent": row.intent,
                "label": row.label,
                "service_team_id": str(row.service_team_id)
                if row.service_team_id is not None
                else None,
            }
            for row in policy.department_mappings
        ],
        "fallback_team_id": str(policy.fallback_team_id)
        if policy.fallback_team_id is not None
        else None,
        "workflow_steps": [
            {
                "position": step.position,
                "action": step.action.value,
                "prompt": step.prompt,
                "required_context": [source.value for source in step.required_context],
                "handoff_on_failure": step.handoff_on_failure,
            }
            for step in policy.workflow_steps
        ],
    }
    if ContextSourceKey.account_health.value in enabled_sources:
        payload["account_health"] = _account_health_payload(context.account_health)
    if ContextSourceKey.profile_completion.value in enabled_sources:
        payload["profile_completion"] = {
            "missing_fields": list(context.profile_missing_fields),
            "may_request_date_of_birth": "date_of_birth"
            in context.profile_missing_fields,
        }
    if ContextSourceKey.access_path.value in enabled_sources:
        payload["access_paths"] = list(context.access_paths)
    return payload


def _system_prompt(policy: IntakePolicy) -> str:
    instructions = (policy.instructions or "").strip()
    configured = f"\nOperator instructions:\n{instructions}" if instructions else ""
    return (
        "You are the Team Inbox intake classifier for a telecom self-care app. "
        "Use only the provided JSON context. Do not invent account, balance, "
        "date-of-birth, service-health, or base-station facts. If the customer "
        "cannot be validated from the context, ask one safe follow-up question. "
        "Return only a JSON object with keys: intent, confidence, "
        "has_enough_information, should_handoff, target_service_team_id, "
        "followup_question, customer_reply, should_close, rationale. "
        "confidence must be 0..1. target_service_team_id must be null or one "
        "of the configured team ids."
        f"{configured}"
    )


def _confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mapped_team_for_intent(policy: IntakePolicy, intent: str) -> UUID | None:
    clean_intent = intent.strip().lower()
    for row in policy.department_mappings:
        if row.intent.strip().lower() == clean_intent and row.service_team_id:
            return row.service_team_id
    for row in policy.department_mappings:
        mapped = row.intent.strip().lower()
        if mapped and mapped in clean_intent and row.service_team_id:
            return row.service_team_id
    return policy.fallback_team_id


def classify_intake(
    db: Session,
    *,
    policy: IntakePolicy,
    context: ConversationAutomationContext,
    inbound_body: str,
) -> IntakeDecision:
    prompt = json.dumps(
        _context_payload(context=context, inbound_body=inbound_body, policy=policy),
        default=str,
        sort_keys=True,
    )
    response, metadata = ai_gateway.generate_with_fallback(
        db,
        system=_system_prompt(policy),
        prompt=prompt,
        max_tokens=900,
    )
    data = parse_json_object(response.content)
    require_keys(
        data,
        [
            "intent",
            "confidence",
            "has_enough_information",
            "should_handoff",
            "should_close",
        ],
    )
    intent = str(data["intent"]).strip().lower()[:80] or "unknown"
    raw_team_id = _uuid_or_none(data.get("target_service_team_id"))
    target_team_id = raw_team_id or _mapped_team_for_intent(policy, intent)
    raw = dict(data)
    raw["provider_metadata"] = metadata
    return IntakeDecision(
        intent=intent,
        confidence=_confidence(data.get("confidence")),
        has_enough_information=bool(data.get("has_enough_information")),
        should_handoff=bool(data.get("should_handoff")),
        target_service_team_id=target_team_id,
        followup_question=_text_or_none(data.get("followup_question")),
        customer_reply=_text_or_none(data.get("customer_reply")),
        should_close=bool(data.get("should_close")),
        rationale=_text_or_none(data.get("rationale")),
        raw=raw,
    )


def classify_intake_safely(
    db: Session,
    *,
    policy: IntakePolicy,
    context: ConversationAutomationContext,
    inbound_body: str,
) -> IntakeDecision | AIClientError:
    try:
        return classify_intake(
            db,
            policy=policy,
            context=context,
            inbound_body=inbound_body,
        )
    except AIClientError as exc:
        return exc


def decimal_to_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
