"""Composable conversational AI intake engine.

The engine owns AI intake state interpretation only. Team Inbox remains the
owner for routing, queueing, assignment, outbound delivery, and human takeover.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from string import Template
from typing import Any

from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.team_inbox import InboxConversation
from app.schemas.ai_intake import AiIntakeClassification
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.network import support_monitoring
from app.services.team_inbox_support_identity import (
    CustomerIdentifierKind,
    CustomerIdentityQuery,
    CustomerIdentityStatus,
    SupportReadContext,
    resolve_customer_identity,
)

STATE_KEY = "conversation_state"
EVENTS_KEY = "conversation_events"

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?:\+?234|0)?[789][01]\d{8}\b")
OUTAGE_CONTEXT_RE = re.compile(
    r"\b(?:since|for)\s+([a-z0-9][a-z0-9\s-]{0,60}?)"
    r"(?=(?:\s+(?:and|but|so|because)\b)|[,.!?;]|$)",
    re.I,
)
PORTAL_RE = re.compile(
    r"\b(?:portal(?:\s+id)?|customer\s+id|account\s+(?:id|number)|acct)\s*"
    r"(?:is|=|[:#-])?\s*([A-Z0-9][A-Z0-9_-]{2,31})\b",
    re.I,
)
HUMAN_RE = re.compile(
    r"\b(?:agent|human|person|operator|representative|customer\s+care|"
    r"speak\s+(?:to|with)\s+someone|talk\s+(?:to|with)\s+someone)\b",
    re.I,
)
APPROVED_HANDOFF_SUMMARY_VARIABLES = frozenset(
    {
        "customer",
        "account",
        "portal_id",
        "channel",
        "issue",
        "intent",
        "category",
        "customer_details",
        "collected_facts",
        "monitoring_findings",
        "troubleshooting",
        "tool_results",
        "escalation_reason",
        "destination_team",
    }
)
SUPPORTED_RULE_CONDITIONS = frozenset(
    {
        "intent",
        "category",
        "customer_identified",
        "field_present",
        "field_value",
        "monitoring_status",
        "tool_result",
        "turn_count",
        "human_requested",
    }
)
SUPPORTED_RULE_ACTIONS = frozenset(
    {
        "request_field",
        "execute_tool",
        "invoke_tool",
        "provide_guidance",
        "respond",
        "handoff",
        "mark_resolved",
    }
)


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    key: str
    display_name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    permission_requirements: tuple[str, ...]
    timeout_seconds: int
    read_only: bool
    allowed_contexts: tuple[str, ...]
    enabled: bool = True


@dataclass(slots=True)
class ConversationalState:
    conversation_id: str
    session_id: str
    policy_version_id: str | None
    channel: str
    current_intent: str | None = None
    previous_intent: str | None = None
    category: str | None = None
    confidence: float | None = None
    subscriber_id: str | None = None
    contact_identity: dict[str, object] = field(default_factory=dict)
    portal_id: str | None = None
    registered_email: str | None = None
    registered_phone: str | None = None
    service_account_identity: dict[str, object] = field(default_factory=dict)
    collected_facts: dict[str, object] = field(default_factory=dict)
    missing_facts: list[str] = field(default_factory=list)
    already_requested_fields: list[str] = field(default_factory=list)
    customer_statements: list[str] = field(default_factory=list)
    troubleshooting_completed: list[str] = field(default_factory=list)
    monitoring_results: list[dict[str, object]] = field(default_factory=list)
    tool_executions: list[dict[str, object]] = field(default_factory=list)
    tool_errors: list[dict[str, object]] = field(default_factory=list)
    resolution_status: str = "open"
    escalation_reason: str | None = None
    destination_team_id: str | None = None
    human_requested: bool = False
    handoff_status: str = "not_requested"
    start_time: str | None = None
    turn_count: int = 0
    clarification_count: int = 0

    @classmethod
    def load(
        cls,
        *,
        conversation: InboxConversation,
        session: AiIntakeSession,
    ) -> ConversationalState:
        raw = dict(session.metadata_ or {}).get(STATE_KEY)
        if isinstance(raw, dict):
            return cls(
                conversation_id=str(raw.get("conversation_id") or conversation.id),
                session_id=str(raw.get("session_id") or session.id),
                policy_version_id=(
                    str(raw["policy_version_id"])
                    if raw.get("policy_version_id")
                    else (
                        str(session.policy_version_id)
                        if session.policy_version_id
                        else None
                    )
                ),
                channel=str(raw.get("channel") or conversation.channel_type),
                current_intent=_text_or_none(raw.get("current_intent")),
                previous_intent=_text_or_none(raw.get("previous_intent")),
                category=_text_or_none(raw.get("category")),
                confidence=_float_or_none(raw.get("confidence")),
                subscriber_id=_text_or_none(raw.get("subscriber_id")),
                contact_identity=_dict(raw.get("contact_identity")),
                portal_id=_text_or_none(raw.get("portal_id")),
                registered_email=_text_or_none(raw.get("registered_email")),
                registered_phone=_text_or_none(raw.get("registered_phone")),
                service_account_identity=_dict(raw.get("service_account_identity")),
                collected_facts=_dict(raw.get("collected_facts")),
                missing_facts=list(_list(raw.get("missing_facts"))),
                already_requested_fields=list(
                    _list(raw.get("already_requested_fields"))
                ),
                customer_statements=list(_list(raw.get("customer_statements"))),
                troubleshooting_completed=list(
                    _list(raw.get("troubleshooting_completed"))
                ),
                monitoring_results=list(_dict_list(raw.get("monitoring_results"))),
                tool_executions=list(_dict_list(raw.get("tool_executions"))),
                tool_errors=list(_dict_list(raw.get("tool_errors"))),
                resolution_status=str(raw.get("resolution_status") or "open"),
                escalation_reason=_text_or_none(raw.get("escalation_reason")),
                destination_team_id=_text_or_none(raw.get("destination_team_id")),
                human_requested=bool(raw.get("human_requested")),
                handoff_status=str(raw.get("handoff_status") or "not_requested"),
                start_time=_text_or_none(raw.get("start_time")),
                turn_count=int(raw.get("turn_count") or 0),
                clarification_count=int(raw.get("clarification_count") or 0),
            )
        return cls(
            conversation_id=str(conversation.id),
            session_id=str(session.id),
            policy_version_id=(
                str(session.policy_version_id) if session.policy_version_id else None
            ),
            channel=conversation.channel_type,
            start_time=datetime.now(UTC).isoformat(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "policy_version_id": self.policy_version_id,
            "channel": self.channel,
            "current_intent": self.current_intent,
            "previous_intent": self.previous_intent,
            "category": self.category,
            "confidence": self.confidence,
            "subscriber_id": self.subscriber_id,
            "contact_identity": self.contact_identity,
            "portal_id": self.portal_id,
            "registered_email": self.registered_email,
            "registered_phone": self.registered_phone,
            "service_account_identity": self.service_account_identity,
            "collected_facts": self.collected_facts,
            "missing_facts": self.missing_facts,
            "already_requested_fields": self.already_requested_fields,
            "customer_statements": self.customer_statements[-12:],
            "troubleshooting_completed": self.troubleshooting_completed,
            "monitoring_results": self.monitoring_results[-8:],
            "tool_executions": self.tool_executions[-20:],
            "tool_errors": self.tool_errors[-12:],
            "resolution_status": self.resolution_status,
            "escalation_reason": self.escalation_reason,
            "destination_team_id": self.destination_team_id,
            "human_requested": self.human_requested,
            "handoff_status": self.handoff_status,
            "start_time": self.start_time,
            "turn_count": self.turn_count,
            "clarification_count": self.clarification_count,
        }


@dataclass(frozen=True, slots=True)
class ConversationEngineDecision:
    action: str
    state: ConversationalState
    response_text: str | None = None
    handoff_summary: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


TOOL_CATALOG: dict[str, ToolDescriptor] = {
    "customer_lookup": ToolDescriptor(
        key="customer_lookup",
        display_name="Customer lookup",
        description="Find a subscriber by an approved customer identifier.",
        input_schema={
            "identifier_type": "portal_id|registered_email|registered_phone",
            "identifier_value": "string",
        },
        output_schema={"status": "found|not_found|ambiguous|unavailable|unauthorized"},
        permission_requirements=("support:ticket:read",),
        timeout_seconds=5,
        read_only=True,
        allowed_contexts=("ai_intake",),
    ),
    "subscriber_monitoring": ToolDescriptor(
        key="subscriber_monitoring",
        display_name="Subscriber monitoring",
        description="Read current subscriber network footprint and session state.",
        input_schema={"subscriber_id": "uuid"},
        output_schema={"status": "available|unavailable|unauthorized"},
        permission_requirements=("support:ticket:read",),
        timeout_seconds=5,
        read_only=True,
        allowed_contexts=("ai_intake",),
    ),
}


def conversational_engine_enabled(version: AiIntakePolicyVersion | None) -> bool:
    metadata = dict(version.metadata_ or {}) if version is not None else {}
    return bool(metadata.get("conversational_engine_enabled"))


def run_conversational_turn(
    db: Session,
    *,
    conversation: InboxConversation,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    latest_body: str,
    classification: AiIntakeClassification | None,
    now: datetime | None = None,
    tool_mode: str = "live_read_only",
) -> ConversationEngineDecision:
    state = ConversationalState.load(conversation=conversation, session=session)
    policy = _policy(version)
    now = now or datetime.now(UTC)
    state.turn_count += 1
    _append_statement(state, latest_body)
    _merge_contact_from_conversation(state, conversation, db)
    facts = extract_facts(latest_body)
    _merge_facts(state, facts)
    _merge_classification(state, classification)

    if state.current_intent != state.previous_intent and state.previous_intent:
        _record_event(session, "intent_changed", now, state=state)

    if state.human_requested:
        return _handoff_decision(
            policy,
            state,
            reason="human_requested",
            response=_handoff_response(
                policy,
                default="I will pass this to a support agent now.",
            ),
        )

    max_turns = _bounded_int(
        policy.get("max_turns"), default=session.max_turns, low=1, high=10
    )
    if state.turn_count > max_turns:
        return _handoff_decision(
            policy,
            state,
            reason="turn_limit",
            response=_handoff_response(
                policy,
                default="I will pass the details I have collected to the support team.",
            ),
        )
    if session.expires_at is not None and session.expires_at <= now:
        return _handoff_decision(
            policy,
            state,
            reason="timeout",
            response=_handoff_response(
                policy,
                default="I will pass this to the support team so they can continue.",
            ),
        )

    _identify_customer(
        db,
        state=state,
        conversation=conversation,
        policy=policy,
        tool_mode=tool_mode,
    )

    if _requires_identity_before_tools(state, policy):
        requested = _next_identifier_to_request(state, policy)
        if requested is not None:
            state.missing_facts = _with_unique(state.missing_facts, requested)
            state.already_requested_fields = _with_unique(
                state.already_requested_fields, requested
            )
            state.clarification_count += 1
            return ConversationEngineDecision(
                action="respond",
                state=state,
                response_text=_identifier_question(requested),
                metadata={"reason": "missing_customer_identifier"},
            )
        return _handoff_decision(
            policy,
            state,
            reason="customer_unidentified",
            response=_handoff_response(
                policy,
                default=(
                    "I could not safely identify the account from the details "
                    "provided. I will pass this to the support team."
                ),
            ),
        )

    if _should_run_monitoring(state, policy):
        result = execute_tool(
            db,
            "subscriber_monitoring",
            {"subscriber_id": state.subscriber_id},
            policy=policy,
            conversation=conversation,
            tool_mode=tool_mode,
        )
        _record_tool_result(state, "subscriber_monitoring", result)
        if result["status"] == "unavailable":
            return _handoff_decision(
                policy,
                state,
                reason="monitoring_unavailable",
                response=_handoff_response(
                    policy,
                    default=(
                        "I could not complete the connection check right now. "
                        "I will pass the details I have collected to the support team."
                    ),
                ),
            )
        if result["status"] == "unauthorized":
            return _handoff_decision(
                policy,
                state,
                reason="monitoring_unauthorized",
                response=_handoff_response(
                    policy,
                    default="I will pass this to the support team for investigation.",
                ),
            )

    rule_decision = _configured_troubleshooting_decision(
        db,
        state,
        policy,
        conversation=conversation,
        tool_mode=tool_mode,
    )
    if rule_decision is not None:
        return rule_decision

    if _technical_issue(state) and _monitoring_offline(state):
        if "los_status" not in state.already_requested_fields:
            state.already_requested_fields = _with_unique(
                state.already_requested_fields, "los_status"
            )
            state.troubleshooting_completed = _with_unique(
                state.troubleshooting_completed, "monitoring_checked"
            )
            return ConversationEngineDecision(
                action="respond",
                state=state,
                response_text=(
                    "Your connection is currently appearing offline from our side. "
                    "Is the router or ONU powered on, and are you seeing any red "
                    "warning light?"
                ),
                metadata={"reason": "troubleshooting_los_check"},
            )

    if _should_handoff_after_classification(state, policy):
        return _handoff_decision(
            policy,
            state,
            reason="classified_ready_for_handoff",
            response=_handoff_response(
                policy,
                default="I have the details needed and will pass this to the right team.",
            ),
        )

    return ConversationEngineDecision(
        action="continue_classifier",
        state=state,
        metadata={"reason": "legacy_classifier_path"},
    )


def persist_state(session: AiIntakeSession, state: ConversationalState) -> None:
    metadata = dict(session.metadata_ or {})
    metadata[STATE_KEY] = state.to_dict()
    session.metadata_ = metadata


def render_handoff_summary(
    state: ConversationalState,
    *,
    version: AiIntakePolicyVersion | None,
    channel: str,
    destination_team_name: str | None = None,
) -> str:
    policy = _policy(version)
    handoff_policy = _dict(policy.get("handoff"))
    template = str(handoff_policy.get("summary_template") or "").strip()
    values = _summary_values(
        state,
        channel=channel,
        destination_team_name=destination_team_name,
    )
    if not template:
        return _default_handoff_summary(values)
    rendered = _render_safe_template(template, values)
    return _strip_empty_summary_lines(rendered)[:2000]


def tool_catalogue_snapshot() -> list[dict[str, object]]:
    return [
        {
            "key": descriptor.key,
            "display_name": descriptor.display_name,
            "description": descriptor.description,
            "input_schema": descriptor.input_schema,
            "output_schema": descriptor.output_schema,
            "permission_requirements": descriptor.permission_requirements,
            "timeout_seconds": descriptor.timeout_seconds,
            "read_only": descriptor.read_only,
            "allowed_contexts": descriptor.allowed_contexts,
            "enabled": descriptor.enabled,
        }
        for descriptor in TOOL_CATALOG.values()
    ]


def extract_facts(text: str) -> dict[str, object]:
    value = str(text or "")
    lowered = value.lower()
    facts: dict[str, object] = {}
    email = EMAIL_RE.search(value)
    if email:
        facts["registered_email"] = normalize_email_identifier(email.group(0))
    phone = PHONE_RE.search(value)
    if phone:
        facts["registered_phone"] = normalize_phone_identifier(phone.group(0))
    portal = PORTAL_RE.search(value)
    if portal:
        facts["portal_id"] = portal.group(1).strip()
    if HUMAN_RE.search(value):
        facts["human_requested"] = True
    if any(
        item in lowered
        for item in ("not browsing", "internet is down", "no internet", "not working")
    ):
        facts["connectivity_problem"] = True
    if "slow" in lowered:
        facts["slow_internet"] = True
    if "restart" in lowered or "reboot" in lowered:
        facts["router_restarted"] = True
    outage_context = OUTAGE_CONTEXT_RE.search(value)
    if outage_context:
        facts["outage_context"] = outage_context.group(0).strip()[:80]
    if (
        "router is on" in lowered
        or "router on" in lowered
        or "powered on" in lowered
        or "router is powered" in lowered
        or "router powered" in lowered
    ):
        facts["router_powered"] = True
    if "los" in lowered and "red" in lowered:
        facts["los_red"] = True
    if "actually" in lowered and "slow" in lowered and "works" in lowered:
        facts["connectivity_problem"] = False
    return facts


def execute_tool(
    db: Session,
    key: str,
    inputs: dict[str, object],
    *,
    policy: dict[str, object],
    conversation: InboxConversation | None = None,
    tool_mode: str = "live_read_only",
) -> dict[str, object]:
    descriptor = TOOL_CATALOG.get(key)
    if descriptor is None or not descriptor.enabled:
        return {"status": "unavailable", "reason": "tool_not_registered"}
    if not _tool_enabled(policy, key):
        return {"status": "unauthorized", "reason": "tool_disabled_by_policy"}
    if tool_mode == "simulation":
        configured_results = policy.get("simulated_tool_results")
        if isinstance(configured_results, dict):
            configured = configured_results.get(key)
            if isinstance(configured, dict):
                return dict(configured)
        return _simulated_tool_result(key, inputs)
    if key == "customer_lookup":
        return _customer_lookup(db, inputs, conversation=conversation)
    if key == "subscriber_monitoring":
        return _subscriber_monitoring(db, inputs)
    return {"status": "unavailable", "reason": "tool_not_implemented"}


def _simulated_tool_result(key: str, inputs: dict[str, object]) -> dict[str, object]:
    if key == "customer_lookup":
        identifier_value = str(inputs.get("identifier_value") or "").strip()
        if not identifier_value:
            return {"status": "not_found", "simulated": True}
        return {
            "status": "found",
            "subscriber_id": "00000000-0000-0000-0000-000000000001",
            "display_name": "Preview customer",
            "account_number": identifier_value,
            "subscriber_status": "preview",
            "simulated": True,
        }
    if key == "subscriber_monitoring":
        return {
            "status": "available",
            "radius_observation": {
                "source": "network.radius_sessions",
                "state": "offline",
                "active_session_count": 0,
                "framed_ip_addresses": [],
                "observed_at": None,
            },
            "ont_observations": [
                {
                    "source": "network.ont_runtime_status",
                    "reference": "preview-ont",
                    "serial_number": None,
                    "effective_state": "offline",
                }
            ],
            "simulated": True,
        }
    return {"status": "unavailable", "reason": "simulation_not_available"}


def _customer_lookup(
    db: Session,
    inputs: dict[str, object],
    *,
    conversation: InboxConversation | None,
) -> dict[str, object]:
    """Verify an identifier only against the trusted Inbox-linked subscriber."""
    if conversation is None:
        return {"status": "unavailable", "reason": "trusted_context_required"}
    identifier_type = str(inputs.get("identifier_type") or "").strip()
    identifier_value = str(inputs.get("identifier_value") or "").strip()
    if not identifier_type or not identifier_value:
        return {"status": "unavailable", "reason": "missing_identifier"}
    identifier_kind = {
        "registered_email": CustomerIdentifierKind.email,
        "registered_phone": CustomerIdentifierKind.phone,
        "portal_id": CustomerIdentifierKind.account_number,
    }.get(identifier_type)
    if identifier_kind is None:
        return {"status": "unauthorized", "reason": "identifier_not_permitted"}
    try:
        result = resolve_customer_identity(
            db,
            CustomerIdentityQuery(
                context=_support_read_context(conversation),
                identifier_kind=identifier_kind,
                identifier_value=identifier_value,
            ),
        )
    except Exception:
        return {"status": "unavailable", "reason": "lookup_failed"}
    if result.status is not CustomerIdentityStatus.found or result.customer is None:
        return {"status": result.status.value}
    return {
        "status": result.status.value,
        "subscriber_id": str(result.customer.subscriber_id),
        "display_name": result.customer.display_name,
        "account_number": result.customer.account_number,
        "subscriber_status": result.customer.status,
    }


def _subscriber_monitoring(db: Session, inputs: dict[str, object]) -> dict[str, object]:
    subscriber_id = coerce_uuid(inputs.get("subscriber_id"))
    if subscriber_id is None:
        return {"status": "unavailable", "reason": "subscriber_required"}
    try:
        projection = support_monitoring.project_support_monitoring(
            db,
            support_monitoring.SupportMonitoringQuery(
                subscriber_id=subscriber_id,
                authorized=True,
            ),
        )
    except Exception:
        return {"status": "unavailable", "reason": "monitoring_query_failed"}
    result: dict[str, object] = {"status": projection.status.value}
    if projection.radius is not None:
        result["radius_observation"] = {
            "source": projection.radius.source,
            "state": projection.radius.state,
            "active_session_count": projection.radius.active_session_count,
            "framed_ip_addresses": list(projection.radius.framed_ip_addresses),
            "observed_at": (
                projection.radius.observed_at.isoformat()
                if projection.radius.observed_at is not None
                else None
            ),
        }
    if projection.onts:
        result["ont_observations"] = [
            {
                "source": observation.source,
                "reference": observation.reference,
                "serial_number": observation.serial_number,
                "effective_state": observation.effective_state,
            }
            for observation in projection.onts
        ]
    return result


def _policy(version: AiIntakePolicyVersion | None) -> dict[str, object]:
    metadata = dict(version.metadata_ or {}) if version is not None else {}
    policy = dict(metadata.get("conversation_policy") or {})
    policy["tools"] = metadata.get("tools") or policy.get("tools") or {}
    policy["permitted_identifiers"] = (
        metadata.get("permitted_identifiers")
        or policy.get("permitted_identifiers")
        or ("registered_phone", "registered_email", "portal_id")
    )
    policy["require_identity_before_tools"] = bool(
        policy.get("require_identity_before_tools", True)
    )
    handoff_policy = _dict(policy.get("handoff"))
    policy["handoff"] = {
        "customer_message": str(handoff_policy.get("customer_message") or "").strip(),
        "summary_template": str(handoff_policy.get("summary_template") or "").strip(),
        "announce_destination": bool(handoff_policy.get("announce_destination")),
    }
    if "troubleshooting_rules" not in policy:
        policy["troubleshooting_rules"] = [
            {
                "condition": {"fact": "los_red", "equals": True},
                "action": "handoff",
                "reason": "red_los",
                "response": (
                    "Thanks for confirming. A red LOS light usually needs "
                    "technical support, so I will pass this to the team now."
                ),
            }
        ]
    return policy


def _merge_contact_from_conversation(
    state: ConversationalState, conversation: InboxConversation, db: Session
) -> None:
    if state.subscriber_id and state.service_account_identity:
        return
    try:
        result = resolve_customer_identity(
            db,
            CustomerIdentityQuery(
                context=_support_read_context(conversation),
                identifier_kind=CustomerIdentifierKind.inbox_linked,
            ),
        )
    except Exception:
        return
    if result.status is not CustomerIdentityStatus.found or result.customer is None:
        return
    customer = result.customer
    state.subscriber_id = str(customer.subscriber_id)
    state.portal_id = state.portal_id or customer.account_number
    state.service_account_identity = {
        "subscriber_id": state.subscriber_id,
        "display_name": customer.display_name,
        "subscriber_status": customer.status,
    }


def _support_read_context(conversation: InboxConversation) -> SupportReadContext:
    """Build owner input from the trusted Team Inbox runtime context, never AI output."""
    return SupportReadContext(
        conversation_id=conversation.id,
        actor_person_id=None,
        can_read_support_context=True,
    )


def _merge_facts(state: ConversationalState, facts: dict[str, object]) -> None:
    for key, value in facts.items():
        if key == "registered_email" and value:
            state.registered_email = str(value)
        elif key == "registered_phone" and value:
            state.registered_phone = str(value)
        elif key == "portal_id" and value:
            state.portal_id = str(value)
        elif key == "human_requested":
            state.human_requested = bool(value)
        else:
            state.collected_facts[key] = value


def _merge_classification(
    state: ConversationalState, classification: AiIntakeClassification | None
) -> None:
    if classification is None:
        return
    next_intent = classification.intent.value
    if state.current_intent and state.current_intent != next_intent:
        state.previous_intent = state.current_intent
    state.current_intent = next_intent
    state.category = classification.category.value
    state.confidence = classification.confidence


def _identify_customer(
    db: Session,
    *,
    state: ConversationalState,
    conversation: InboxConversation,
    policy: dict[str, object],
    tool_mode: str,
) -> None:
    if state.subscriber_id:
        return
    for identifier_type, value in (
        ("registered_email", state.registered_email),
        ("registered_phone", state.registered_phone),
        ("portal_id", state.portal_id),
    ):
        if not value or identifier_type not in _permitted_identifiers(policy):
            continue
        result = execute_tool(
            db,
            "customer_lookup",
            {
                "identifier_type": identifier_type,
                "identifier_value": value,
                "conversation_id": str(conversation.id),
            },
            policy=policy,
            conversation=conversation,
            tool_mode=tool_mode,
        )
        _record_tool_result(state, "customer_lookup", result)
        if result.get("status") == "found":
            state.subscriber_id = str(result["subscriber_id"])
            state.service_account_identity = {
                "subscriber_id": state.subscriber_id,
                "display_name": result.get("display_name"),
                "subscriber_status": result.get("subscriber_status"),
            }
            return
        if result.get("status") in {"ambiguous", "unavailable", "unauthorized"}:
            return


def _record_tool_result(
    state: ConversationalState, key: str, result: dict[str, object]
) -> None:
    result_payload: dict[str, object] = {
        item_key: item_value
        for item_key, item_value in result.items()
        if item_key not in {"email", "phone"}
    }
    entry: dict[str, object] = {
        "tool": key,
        "status": result.get("status"),
        "at": datetime.now(UTC).isoformat(),
        "result": result_payload,
    }
    state.tool_executions.append(entry)
    if result.get("status") in {"unavailable", "unauthorized"}:
        state.tool_errors.append(entry)
    if key == "subscriber_monitoring" and result.get("status") == "available":
        state.monitoring_results.append(result_payload)


def _requires_identity_before_tools(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    return (
        _technical_issue(state)
        and not state.subscriber_id
        and bool(policy.get("require_identity_before_tools", True))
    )


def _should_run_monitoring(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    if not _technical_issue(state) or not state.subscriber_id:
        return False
    if any(
        item.get("tool") == "subscriber_monitoring" for item in state.tool_executions
    ):
        return False
    return _tool_enabled(policy, "subscriber_monitoring")


def _technical_issue(state: ConversationalState) -> bool:
    return state.current_intent == "technical_support" or bool(
        state.collected_facts.get("connectivity_problem")
        or state.collected_facts.get("slow_internet")
    )


def _monitoring_offline(state: ConversationalState) -> bool:
    latest = state.monitoring_results[-1] if state.monitoring_results else {}
    radius = latest.get("radius_observation")
    return isinstance(radius, dict) and radius.get("state") == "offline"


def _configured_troubleshooting_decision(
    db: Session,
    state: ConversationalState,
    policy: dict[str, object],
    *,
    conversation: InboxConversation,
    tool_mode: str,
) -> ConversationEngineDecision | None:
    rules = policy.get("troubleshooting_rules")
    if not isinstance(rules, list):
        return None
    for raw in rules:
        if not isinstance(raw, dict):
            continue
        condition = raw.get("condition")
        if not isinstance(condition, dict):
            continue
        if not _condition_matches(state, condition):
            continue
        action = str(raw.get("action") or "").strip()
        if action in {"execute_tool", "invoke_tool"}:
            tool_key = str(raw.get("tool") or "").strip()
            if not tool_key:
                continue
            if any(item.get("tool") == tool_key for item in state.tool_executions):
                continue
            inputs: dict[str, object] = {}
            if tool_key == "subscriber_monitoring":
                if not state.subscriber_id:
                    continue
                inputs["subscriber_id"] = state.subscriber_id
            result = execute_tool(
                db,
                tool_key,
                inputs,
                policy=policy,
                conversation=conversation,
                tool_mode=tool_mode,
            )
            _record_tool_result(state, tool_key, result)
            continue
        if action in {"respond", "provide_guidance"}:
            response = str(raw.get("response") or "").strip()
            if response:
                return ConversationEngineDecision(
                    action="respond",
                    state=state,
                    response_text=response[:800],
                    metadata={"reason": "troubleshooting_guidance"},
                )
        if action == "request_field":
            field = str(raw.get("field") or raw.get("tool") or "").strip()
            if field and field not in state.already_requested_fields:
                state.missing_facts = _with_unique(state.missing_facts, field)
                state.already_requested_fields = _with_unique(
                    state.already_requested_fields, field
                )
                state.clarification_count += 1
                return ConversationEngineDecision(
                    action="respond",
                    state=state,
                    response_text=str(
                        raw.get("response") or _identifier_question(field)
                    ),
                    metadata={"reason": "troubleshooting_required_field"},
                )
        if action == "mark_resolved":
            state.resolution_status = "resolved"
            response = str(raw.get("response") or "").strip()
            return ConversationEngineDecision(
                action="respond",
                state=state,
                response_text=response
                or "Thanks. I have recorded this as resolved from the details provided.",
                metadata={"reason": "troubleshooting_resolved"},
            )
        if action == "handoff":
            return _handoff_decision(
                policy,
                state,
                reason=str(raw.get("reason") or "troubleshooting_rule"),
                response=_handoff_response(
                    policy,
                    default=str(
                        raw.get("response")
                        or "I will pass this to the support team for investigation."
                    ),
                ),
            )
    return None


def _condition_matches(
    state: ConversationalState, condition: dict[str, object]
) -> bool:
    condition_type = str(condition.get("type") or "").strip()
    if condition_type == "intent":
        return _compare_value(
            state.current_intent,
            {**condition, "value": condition.get("value", condition.get("intent"))},
        )
    if condition_type == "category":
        return _compare_value(
            state.category,
            {**condition, "value": condition.get("value", condition.get("category"))},
        )
    if condition_type == "customer_identified":
        return bool(state.subscriber_id) is bool(condition.get("customer_identified"))
    if condition_type == "human_requested":
        return state.human_requested is bool(condition.get("human_requested"))
    if condition_type == "turn_count":
        return _compare_number(state.turn_count, condition)
    if condition_type == "field_present":
        field = str(condition.get("field") or "").strip()
        return _field_value(state, field) not in (None, "", False)
    if condition_type == "field_value":
        return _compare_value(
            _field_value(state, str(condition.get("field") or "").strip()),
            condition,
        )
    if condition_type == "monitoring_status":
        latest = state.monitoring_results[-1] if state.monitoring_results else {}
        return _compare_value(
            latest.get("service_state"),
            {
                **condition,
                "value": condition.get("value", condition.get("monitoring_status")),
            },
        )
    if condition_type == "tool_result":
        tool = str(condition.get("tool") or "").strip()
        status = str(condition.get("status") or condition.get("value") or "").strip()
        return any(
            item.get("tool") == tool and str(item.get("status") or "") == status
            for item in state.tool_executions
        )
    recognized = False
    fact_key = str(condition.get("fact") or "").strip()
    if fact_key:
        recognized = True
        value = state.collected_facts.get(fact_key)
        expected = condition.get("equals")
        return value == expected if "equals" in condition else bool(value)
    intent = condition.get("intent")
    if intent is not None:
        recognized = True
    if intent is not None and state.current_intent != str(intent):
        return False
    category = condition.get("category")
    if category is not None:
        recognized = True
    if category is not None and state.category != str(category):
        return False
    return recognized


def _field_value(state: ConversationalState, field: str) -> object:
    if field in {"portal_id", "registered_email", "registered_phone"}:
        return getattr(state, field)
    return state.collected_facts.get(field)


def _compare_value(value: object, condition: dict[str, object]) -> bool:
    expected = condition.get("value", condition.get("equals"))
    operator = str(condition.get("operator") or "equals").strip()
    if operator in {"is", "equals", "=="}:
        return str(value or "").strip() == str(expected or "").strip()
    if operator == "contains":
        return str(expected or "").strip().lower() in str(value or "").lower()
    if operator == "present":
        return value not in (None, "", False)
    return False


def _compare_number(value: int, condition: dict[str, object]) -> bool:
    try:
        expected = int(str(condition.get("turn_count", condition.get("value", 0))))
    except (TypeError, ValueError):
        return False
    operator = str(condition.get("operator") or ">=").strip()
    if operator == ">=":
        return value >= expected
    if operator == ">":
        return value > expected
    if operator in {"=", "==", "equals"}:
        return value == expected
    if operator == "<=":
        return value <= expected
    if operator == "<":
        return value < expected
    return False


def _should_handoff_after_classification(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    if policy.get("handoff_after_classification") is False:
        return False
    return bool(state.current_intent and state.confidence is not None)


def _handoff_decision(
    policy: dict[str, object], state: ConversationalState, *, reason: str, response: str
) -> ConversationEngineDecision:
    state.escalation_reason = reason
    state.handoff_status = "requested"
    state.resolution_status = "escalated"
    return ConversationEngineDecision(
        action="handoff",
        state=state,
        response_text=response,
        handoff_summary=render_handoff_summary(
            state,
            version=None,
            channel=state.channel,
            destination_team_name=None,
        )
        if not _dict(policy.get("handoff")).get("summary_template")
        else None,
        metadata={"reason": reason},
    )


def _handoff_response(policy: dict[str, object], *, default: str) -> str:
    configured = _dict(policy.get("handoff")).get("customer_message")
    text = str(configured or "").strip()
    return text[:800] if text else default


def _summary_values(
    state: ConversationalState,
    *,
    channel: str,
    destination_team_name: str | None,
) -> dict[str, str]:
    details = "\n".join(f"- {item}" for item in state.customer_statements[-5:])
    facts = ", ".join(
        f"{key}={value}" for key, value in sorted(state.collected_facts.items())
    )
    latest_monitoring = state.monitoring_results[-1] if state.monitoring_results else {}
    monitoring = ""
    if latest_monitoring:
        monitoring = "; ".join(
            f"{key}={value}"
            for key, value in (
                ("status", latest_monitoring.get("status")),
                ("service_state", latest_monitoring.get("service_state")),
                ("radius_online", latest_monitoring.get("radius_online")),
            )
            if value is not None
        )
    tool_results = []
    for execution in state.tool_executions[-6:]:
        result = _dict(execution.get("result"))
        tool_results.append(
            f"{execution.get('tool')}: {execution.get('status')}"
            + (f" ({result.get('reason')})" if result.get("reason") is not None else "")
        )
    customer = (
        str(state.service_account_identity.get("display_name") or "").strip()
        or str(state.contact_identity.get("display_name") or "").strip()
    )
    account = (
        state.portal_id
        or str(state.service_account_identity.get("subscriber_id") or "").strip()
    )
    issue = state.category or state.current_intent or facts
    return {
        "customer": customer,
        "account": account,
        "portal_id": state.portal_id or "",
        "channel": channel,
        "issue": str(issue or ""),
        "intent": state.current_intent or "",
        "category": state.category or "",
        "customer_details": details,
        "collected_facts": facts,
        "monitoring_findings": monitoring,
        "troubleshooting": ", ".join(state.troubleshooting_completed),
        "tool_results": "\n".join(f"- {item}" for item in tool_results),
        "escalation_reason": state.escalation_reason or "",
        "destination_team": destination_team_name or state.destination_team_id or "",
    }


def _default_handoff_summary(values: dict[str, str]) -> str:
    sections = [
        ("AI Intake Summary", "AI Intake Summary"),
        ("Customer", values["customer"]),
        ("Account / Portal ID", values["account"] or values["portal_id"]),
        ("Channel", values["channel"]),
        ("Issue", values["issue"]),
        ("Detected intent", values["intent"]),
        ("Customer-provided details", values["customer_details"]),
        ("Monitoring findings", values["monitoring_findings"]),
        ("Troubleshooting already performed", values["troubleshooting"]),
        ("Relevant tool results", values["tool_results"]),
        ("Reason for escalation", values["escalation_reason"]),
        ("Recommended destination", values["destination_team"]),
    ]
    lines: list[str] = []
    for label, value in sections:
        if label == value:
            lines.append(value)
        elif value:
            lines.append(f"{label}: {value}")
    return "\n\n".join(lines)[:2000]


def _render_safe_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    try:
        rendered = Template(rendered).safe_substitute(values)
    except ValueError:
        pass
    return rendered


def _strip_empty_summary_lines(text: str) -> str:
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if ":" in line and not line.split(":", 1)[1].strip():
            continue
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _identifier_question(identifier_type: str) -> str:
    if identifier_type == "registered_email":
        return "Please send the registered email on the account so I can identify it."
    if identifier_type == "registered_phone":
        return "Please send the registered phone number on the account."
    return "Please send your Portal ID or account number so I can identify the service."


def _next_identifier_to_request(
    state: ConversationalState, policy: dict[str, object]
) -> str | None:
    supplied = {
        "registered_email": bool(state.registered_email),
        "registered_phone": bool(state.registered_phone),
        "portal_id": bool(state.portal_id),
    }
    for identifier in _permitted_identifiers(policy):
        if supplied.get(identifier):
            continue
        if identifier in state.already_requested_fields:
            continue
        return identifier
    return None


def _permitted_identifiers(policy: dict[str, object]) -> tuple[str, ...]:
    raw = policy.get("permitted_identifiers")
    if isinstance(raw, str):
        raw = [raw]
    allowed = {
        "portal_id",
        "registered_email",
        "registered_phone",
    }
    return tuple(item for item in _list(raw) if item in allowed) or (
        "registered_phone",
        "registered_email",
        "portal_id",
    )


def _tool_enabled(policy: dict[str, object], key: str) -> bool:
    tools = policy.get("tools")
    if tools is None:
        return key == "customer_lookup"
    if isinstance(tools, dict):
        raw = tools.get(key)
        if isinstance(raw, dict):
            return bool(raw.get("enabled", False))
        return bool(raw)
    if isinstance(tools, list):
        return key in tools
    return False


def _append_statement(state: ConversationalState, text: str) -> None:
    clean = " ".join(str(text or "").split())[:500]
    if clean:
        state.customer_statements.append(clean)


def _record_event(
    session: AiIntakeSession,
    event_type: str,
    at: datetime,
    *,
    state: ConversationalState,
) -> None:
    metadata = dict(session.metadata_ or {})
    events = list(_dict_list(metadata.get(EVENTS_KEY)))
    events.append(
        {
            "event_type": event_type,
            "at": at.isoformat(),
            "current_intent": state.current_intent,
            "previous_intent": state.previous_intent,
        }
    )
    metadata[EVENTS_KEY] = events[-40:]
    session.metadata_ = metadata


def _with_unique(values: list[str], value: str) -> list[str]:
    return list(dict.fromkeys([*values, value]))


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bounded_int(value: object, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value)) if value is not None else int(str(default))
    except (TypeError, ValueError):
        parsed = int(str(default))
    return max(low, min(parsed, high))
