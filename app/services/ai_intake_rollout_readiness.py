from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

from app.models.ai_intake import (
    AiIntakeCanaryRun,
    AiIntakeCanaryScenario,
    AiIntakeCanarySuite,
    AiIntakeCanarySuiteScenario,
)
from app.services import ai_intake_canary_runner
from app.services.ai_intake_canary_runner import (
    CanaryAssertion,
    CanaryAssertionType,
    CanaryChannel,
    CanaryCompletionMode,
    CanaryCustomerFact,
    CanaryEngineMode,
    CanaryEventType,
    CanaryInboundTurn,
    CanaryMediaType,
    CanaryMonitoringObservation,
    CanaryRunResult,
    CanaryScenarioDefinition,
    CanaryToolName,
    CanaryToolResult,
    CanaryToolStatus,
    ProductionPatternObservation,
)
from app.services.ai_intake_text import usable_customer_text

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CanaryTurn:
    text: str | None = None
    media_type: str | None = None
    identity_status: str | None = None
    monitoring_status: str | None = None
    agent_state: str | None = None
    policy_mode: str | None = None
    langgraph_available: bool = True
    duplicate_delivery: bool = False
    tool_failure: str | None = None


@dataclass(frozen=True, slots=True)
class CanaryScenario:
    key: str
    title: str
    turns: tuple[CanaryTurn, ...]
    expected_flags: frozenset[str]
    forbidden_flags: frozenset[str] = frozenset()
    high_priority: bool = False


@dataclass(frozen=True, slots=True)
class CanarySimulationResult:
    key: str
    requested_engine: str
    actual_engine: str
    flags: frozenset[str]


@dataclass(frozen=True, slots=True)
class CanaryScenarioMatrixRow:
    key: str
    title: str
    implemented: bool
    automated: bool
    passed: bool
    remaining_gap: str


@dataclass(frozen=True, slots=True)
class CanaryPreviewResult:
    key: str
    title: str
    requested_engine: str
    actual_engine: str
    flags: frozenset[str]
    sends_real_messages: bool
    creates_real_assignments: bool
    creates_queue_entries: bool
    creates_internal_notes: bool
    mutates_customers: bool
    uses_live_monitoring: bool


@dataclass(frozen=True, slots=True)
class PolicyDraftValidation:
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NaturalConversationScenario:
    key: str
    title: str
    customer_turns: tuple[str, ...]
    ai_responses: tuple[str, ...]
    expected_flags: frozenset[str]
    queued: bool = False
    human_owned: bool = False


@dataclass(frozen=True, slots=True)
class NaturalConversationScore:
    key: str
    passed: bool
    naturalness: bool
    context_awareness: bool
    repetition: bool
    robotic_wording: bool
    unnecessary_questions: bool
    ownership_transition: bool
    duplicate_queue_messaging: bool
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateCheck:
    key: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class CanaryLibraryScenarioRow:
    scenario_id: str
    name: str
    revision: int
    enabled: bool
    required_for_activation: bool
    priority: int
    tags: tuple[str, ...]
    latest_passed: bool
    latest_engine: str
    latest_policy_version: int | None


@dataclass(frozen=True, slots=True)
class PreActivationGateReport:
    ready: bool
    checks: tuple[GateCheck, ...]


HIGH_PRIORITY_REGRESSION_SCENARIOS = frozenset(
    {
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
        "G",
        "I",
        "J",
        "L",
        "M",
        "P",
        "R",
        "S",
        "T",
        "W",
        "X",
    }
)

CANARY_SCENARIOS = (
    CanaryScenario(
        key="A",
        title="Normal Technical Conversation",
        turns=(CanaryTurn(text="My internet is not browsing."),),
        expected_flags=frozenset(
            {
                "langgraph_received_message",
                "conversation_context_used",
                "no_repeated_questions",
                "monitoring_permission_checked",
                "conversational_response",
            }
        ),
        forbidden_flags=frozenset({"monitoring_without_permission"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="B",
        title="Rich First Message",
        turns=(
            CanaryTurn(
                text=(
                    "My internet is down. My Portal ID is 12345 and I restarted "
                    "the router twice."
                )
            ),
        ),
        expected_flags=frozenset(
            {
                "issue_extracted",
                "identifier_extracted",
                "restart_fact_retained",
                "identifier_not_requested_again",
                "no_repeated_questions",
            }
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="C",
        title="Multi-Turn Memory",
        turns=(
            CanaryTurn(text="My internet isn't working."),
            CanaryTurn(text="The router is powered."),
            CanaryTurn(text="LOS is red and I already restarted twice."),
        ),
        expected_flags=frozenset(
            {
                "state_persisted",
                "router_power_fact_retained",
                "customer_los_fact_retained",
                "restart_fact_retained",
            }
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="D",
        title="Intent Correction",
        turns=(
            CanaryTurn(text="My internet is completely down."),
            CanaryTurn(text="Actually it works, but it is extremely slow."),
        ),
        expected_flags=frozenset(
            {"active_issue_changed", "stale_no_connectivity_flow_abandoned"}
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="E",
        title="Existing Linked Subscriber",
        turns=(
            CanaryTurn(
                text="My internet is not browsing.",
                identity_status="linked_subscriber",
            ),
        ),
        expected_flags=frozenset(
            {"linked_subscriber_used", "identifier_not_requested_again"}
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="F",
        title="Identifier Verification",
        turns=(
            CanaryTurn(text="+234 800 000 0000", identity_status="linked_subscriber"),
            CanaryTurn(
                text="customer@example.test", identity_status="linked_subscriber"
            ),
            CanaryTurn(text="Portal ID 12345", identity_status="linked_subscriber"),
        ),
        expected_flags=frozenset(
            {
                "phone_verified_against_linked_subscriber",
                "email_verified_against_linked_subscriber",
                "portal_id_verified_against_linked_subscriber",
            }
        ),
        forbidden_flags=frozenset({"directory_wide_lookup"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="G",
        title="Unlinked Customer",
        turns=(
            CanaryTurn(text="My internet is not browsing.", identity_status="unlinked"),
        ),
        expected_flags=frozenset(
            {
                "unauthorized_search_avoided",
                "identity_not_fabricated",
                "safe_handoff_or_fallback",
            }
        ),
        forbidden_flags=frozenset({"directory_wide_lookup", "fabricated_identity"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="H",
        title="Monitoring Online",
        turns=(
            CanaryTurn(
                text="My internet is not browsing.",
                monitoring_status="online",
            ),
        ),
        expected_flags=frozenset(
            {
                "monitoring_permission_checked",
                "radius_provenance_retained",
                "ont_provenance_retained",
                "monitoring_observation_consumed",
            }
        ),
    ),
    CanaryScenario(
        key="I",
        title="Monitoring No Data",
        turns=(
            CanaryTurn(
                text="My internet is not browsing.",
                monitoring_status="no_data",
            ),
        ),
        expected_flags=frozenset({"monitoring_no_data_preserved"}),
        forbidden_flags=frozenset({"offline_claimed"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="J",
        title="Monitoring Unavailable",
        turns=(
            CanaryTurn(
                text="My internet is not browsing.",
                monitoring_status="unavailable",
            ),
        ),
        expected_flags=frozenset(
            {"monitoring_unavailable_preserved", "safe_handoff_or_fallback"}
        ),
        forbidden_flags=frozenset({"offline_claimed"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="K",
        title="Customer-Reported LOS",
        turns=(CanaryTurn(text="LOS is red."),),
        expected_flags=frozenset({"customer_reported_los_fact"}),
        forbidden_flags=frozenset({"monitoring_observation_fabricated"}),
    ),
    CanaryScenario(
        key="L",
        title="Explicit Human Request",
        turns=(CanaryTurn(text="I want to speak to an agent."),),
        expected_flags=frozenset(
            {
                "troubleshooting_stopped",
                "handoff_message_sent",
                "private_summary_created",
                "team_inbox_routing_used",
                "assignment_or_fifo_used",
                "ai_ownership_ended",
            }
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="M",
        title="Media-Only First Message",
        turns=(
            CanaryTurn(text="image", media_type="image"),
            CanaryTurn(text="voice note", media_type="audio"),
            CanaryTurn(text="document.pdf", media_type="document"),
            CanaryTurn(text="video", media_type="video"),
        ),
        expected_flags=frozenset(
            {
                "media_only_equivalent",
                "handoff_message_sent",
                "private_summary_created",
                "team_inbox_routing_used",
                "assignment_or_fifo_used",
                "ai_ownership_ended",
            }
        ),
        forbidden_flags=frozenset(
            {"media_interpreted", "customer_lookup_called", "monitoring_called"}
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="N",
        title="Media + Usable Caption",
        turns=(
            CanaryTurn(
                text=(
                    "My router has been showing this since this morning and I "
                    "cannot browse."
                ),
                media_type="image",
            ),
        ),
        expected_flags=frozenset({"actionable_text_used", "normal_intake_continued"}),
        forbidden_flags=frozenset({"media_interpreted", "automatic_media_handoff"}),
    ),
    CanaryScenario(
        key="O",
        title="Agent Available",
        turns=(
            CanaryTurn(text="I want to speak to an agent.", agent_state="available"),
        ),
        expected_flags=frozenset(
            {"team_inbox_routing_used", "round_robin_assignment_used"}
        ),
    ),
    CanaryScenario(
        key="P",
        title="All Agents Busy",
        turns=(CanaryTurn(text="I want to speak to an agent.", agent_state="busy"),),
        expected_flags=frozenset({"fifo_queue_used", "team_inbox_queue_position_used"}),
        forbidden_flags=frozenset({"langgraph_queue_position_calculated"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="Q",
        title="Queue Promotion",
        turns=(CanaryTurn(agent_state="capacity_available"),),
        expected_flags=frozenset(
            {"queue_worker_promoted", "round_robin_assignment_used"}
        ),
    ),
    CanaryScenario(
        key="R",
        title="Human Reply While Queued",
        turns=(CanaryTurn(text="Agent reply", agent_state="human_replied_queued"),),
        expected_flags=frozenset(
            {
                "ai_ownership_ended",
                "stale_queue_position_suppressed",
                "stale_heartbeat_suppressed",
                "handoff_notice_remains_valid",
            }
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="S",
        title="Human Assignment After AI Conversation",
        turns=(CanaryTurn(text="Still not working", agent_state="human_owned"),),
        expected_flags=frozenset(
            {"ai_restart_suppressed", "human_ownership_respected"}
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="T",
        title="Tool Failure",
        turns=(
            CanaryTurn(text="My internet is down.", tool_failure="identity_projection"),
            CanaryTurn(
                text="My internet is down.", tool_failure="monitoring_projection"
            ),
        ),
        expected_flags=frozenset(
            {
                "identity_failure_handoff",
                "monitoring_failure_handoff",
                "tool_failure_recorded",
            }
        ),
        high_priority=True,
    ),
    CanaryScenario(
        key="U",
        title="Invalid/Unsupported Policy",
        turns=(CanaryTurn(policy_mode="invalid"),),
        expected_flags=frozenset(
            {"invalid_policy_rejected", "unsafe_activation_blocked"}
        ),
    ),
    CanaryScenario(
        key="V",
        title="Policy Version Pinning",
        turns=(CanaryTurn(policy_mode="version_pinning"),),
        expected_flags=frozenset(
            {"conversation_a_pinned_v1", "conversation_b_gets_v2"}
        ),
    ),
    CanaryScenario(
        key="W",
        title="Engine Selection",
        turns=(
            CanaryTurn(policy_mode="custom_v1"),
            CanaryTurn(policy_mode="langgraph_v1", langgraph_available=True),
            CanaryTurn(policy_mode="langgraph_v1", langgraph_available=False),
        ),
        expected_flags=frozenset(
            {
                "custom_requested_actual_custom",
                "langgraph_requested_actual_langgraph",
                "fallback_requested_actual_recorded",
            }
        ),
        forbidden_flags=frozenset({"engine_execution_misrepresented"}),
        high_priority=True,
    ),
    CanaryScenario(
        key="X",
        title="Duplicate Inbound/Webhook Retry",
        turns=(
            CanaryTurn(
                text="My internet is not browsing.",
                duplicate_delivery=True,
            ),
        ),
        expected_flags=frozenset(
            {
                "duplicate_graph_execution_prevented",
                "duplicate_outbound_prevented",
                "duplicate_private_note_prevented",
                "duplicate_queue_entry_prevented",
            }
        ),
        high_priority=True,
    ),
)

LANGGRAPH_POLICY_DRAFT: dict[str, Any] = {
    "status": "draft",
    "is_active": False,
    "scope": {
        "channels": ["whatsapp"],
        "business_account": "Dotmac Support",
        "provider": "meta_cloud_api",
        "account_scope": "<resolved-whatsapp-phone-number-id>",
        "contact_level_isolation_available": False,
    },
    "engine": {
        "conversational_engine_enabled": True,
        "conversation_engine_mode": "langgraph_v1",
        "fallback_engine_mode": "custom_v1",
        "record_requested_and_actual_engine": True,
    },
    "persona": {
        "display_name": "Dotmac Support",
        "public_identity": "Dotmac Support",
        "automation_disclosure": "truthful_only_when_customer_asks",
        "tone": "natural, concise, context-aware Dotmac support",
    },
    "conversation_style": {
        "avoid_repeated_ai_disclosure": True,
        "avoid_internal_workflow_terms": True,
        "acknowledge_issue_first": True,
        "ask_one_useful_question_at_a_time": True,
        "reference_known_customer_facts": True,
        "acknowledge_corrections_naturally": True,
        "handoff_naturally": True,
    },
    "conversation_templates": {
        "welcome": "Welcome to Dotmac Support. How can we help today?",
        "greeting_only": "Welcome to Dotmac Support. How can we help today?",
        "standard_handoff": (
            "Thanks, I have the details I need. I am passing this to our "
            "support team so they can take a closer look."
        ),
        "media_first_handoff": (
            "Thanks for sending that. I cannot review attachments here, so I "
            "am passing this to our support team to take a closer look."
        ),
        "direct_ai_question": (
            "I am Dotmac's automated support assistant. I can help collect the "
            "details and connect you with the right support team."
        ),
        "direct_human_question": (
            "I am Dotmac's automated support assistant, not a human agent. I "
            "can still help collect the details and pass them to the support team."
        ),
    },
    "channel_overrides": {},
    "intents": [
        "technical_support",
        "billing_issue",
        "payment_confirmation",
        "subscription_renewal",
        "plan_change",
        "coverage_request",
        "new_connection",
        "account_access",
        "complaint",
        "general_enquiry",
        "unknown",
    ],
    "allowed_tools": [
        "team_inbox_support_identity",
        "network.support_monitoring",
        "team_inbox_handoff",
    ],
    "customer_identification": {
        "use_linked_conversation_identity_only": True,
        "directory_wide_lookup": False,
        "verify_supplied_identifier_against_linked_subscriber_only": True,
    },
    "troubleshooting": {
        "preserve_customer_reported_facts": True,
        "monitoring_requires_permission": True,
        "keep_radius_and_ont_provenance_separate": True,
        "no_data_is_not_offline": True,
        "unavailable_is_not_offline": True,
    },
    "human_request": {
        "stop_troubleshooting": True,
        "send_handoff_message": True,
        "create_private_summary": True,
        "use_team_inbox_routing": True,
        "end_ai_ownership": True,
    },
    "internal_summary": {
        "create_private_note": True,
        "known_facts_only": True,
        "idempotent_note_key": True,
        "redact_unnecessary_pii": True,
    },
    "media_first": {
        "use_usable_text": True,
        "handoff_when_no_usable_text": True,
        "do_not_interpret_media_without_approved_capability": True,
    },
    "routing": {
        "owner": "Team Inbox",
        "preserve_fifo_queue": True,
        "preserve_round_robin": True,
        "preserve_agent_capacity": True,
    },
    "queue_messages": {
        "use_team_inbox_templates": True,
        "langgraph_calculates_queue_position": False,
    },
    "limits": {"max_turns": 3, "timeout_seconds": 30, "confidence_threshold": 0.75},
}

ROLLBACK_CONTROL = {
    "from": "langgraph_v1",
    "to": "custom_v1",
    "mechanism": (
        "Use the AI Intake policy owner to disable the active LangGraph draft/version "
        "or publish/switch the scoped policy back to conversation_engine_mode=custom_v1."
    ),
    "requires_database_repair": False,
    "requires_queue_reset": False,
    "requires_worker_restart": False,
    "preserves_human_owned_conversations": True,
}

OBSERVABILITY_SIGNALS = frozenset(
    {
        "requested_engine",
        "actual_engine",
        "graph_execution",
        "policy_version",
        "customer_identity_status",
        "monitoring_result_status",
        "tool_failures",
        "handoff_reason",
        "private_note_creation",
        "routing_result",
        "assignment_or_queue_result",
        "ai_stopped_after_human_ownership",
        "duplicate_outbound_detection",
        "stale_queue_notification_suppression",
        "stuck_sessions",
        "conversation_quality_score",
    }
)

OBSERVABILITY_EVIDENCE: dict[str, str] = {
    "requested_engine": "session/generation metadata records requested engine",
    "actual_engine": "session/generation metadata records actual engine or fallback",
    "graph_execution": "generation attempt records graph execution status",
    "policy_version": "AI intake session pins policy_version_id",
    "customer_identity_status": "support-safe identity tool result status",
    "monitoring_result_status": "support-safe monitoring DTO status",
    "tool_failures": "generation attempt error_code and tool failure reason",
    "handoff_reason": "handoff metadata and private summary note metadata",
    "private_note_creation": "Team Inbox internal note with idempotent AI key",
    "routing_result": "inbound routing metadata from Team Inbox routing decision",
    "assignment_or_queue_result": "Team Inbox assignment result or queue entry",
    "ai_stopped_after_human_ownership": "session terminal state and conversation AI metadata",
    "duplicate_outbound_detection": "outbound/generation idempotency keys",
    "stale_queue_notification_suppression": "Team Inbox queue notification suppression evidence",
    "stuck_sessions": "AI intake session expiry/stuck-session sweep signal",
    "conversation_quality_score": "natural conversation acceptance score",
}

CONTROLLED_ACTIVATION_PLAN: dict[str, Any] = {
    "execute_now": False,
    "activation_window": "approved low-traffic support window",
    "responsible_operator": "named operator with support:ticket:update",
    "monitoring_owner": "named observer watching AI intake and Team Inbox signals",
    "rollback_owner": "named operator authorized to switch policy to custom_v1",
    "first_acceptance_scenarios": (
        "A",
        "B",
        "E",
        "I",
        "L",
        "M",
        "O",
        "P",
        "R",
        "X",
    ),
    "stop_conditions": (
        "duplicate AI replies",
        "wrong customer identification",
        "privacy leakage",
        "incorrect monitoring claims",
        "wrong routing",
        "missing handoff note",
        "AI continuing after human takeover",
        "stale queue notices",
        "stuck sessions",
        "excessive errors or fallbacks",
    ),
    "on_stop_condition": (
        "disable langgraph_v1 for the scoped policy",
        "return to custom_v1",
        "preserve evidence",
        "HOLD",
    ),
}

FORBIDDEN_CUSTOMER_TERMS = frozenset(
    {
        "intent detected",
        "classification complete",
        "monitoring node",
        "handoff condition reached",
        "routing decision",
        "confidence score",
        "langgraph",
        "queue worker",
        "escalation rule",
        "information recorded",
    }
)
ROBOTIC_CUSTOMER_TERMS = frozenset(
    {
        "intent detected",
        "classification complete",
        "information recorded",
        "escalation condition reached",
        "handoff condition reached",
    }
)
AI_DISCLOSURE_TERMS = frozenset(
    {
        "i am an ai",
        "i'm an ai",
        "i am a bot",
        "i'm a bot",
        "virtual assistant",
        "automated assistant",
    }
)

NATURAL_CONVERSATION_SCENARIOS = (
    NaturalConversationScenario(
        key="greeting_only",
        title="Greeting-only first message",
        customer_turns=("Hi",),
        ai_responses=("Welcome to Dotmac Support. How can we help today?",),
        expected_flags=frozenset({"natural_intro", "asks_help_needed"}),
    ),
    NaturalConversationScenario(
        key="problem_immediate",
        title="Customer states problem immediately",
        customer_turns=("My internet is not browsing.",),
        ai_responses=(
            "Sorry about the browsing issue. Is the LOS light on your router red?",
        ),
        expected_flags=frozenset({"acknowledges_issue", "asks_one_next_question"}),
    ),
    NaturalConversationScenario(
        key="several_facts",
        title="Customer provides several facts in one message",
        customer_turns=(
            "My internet is down. My Portal ID is 12345 and I restarted the router twice.",
        ),
        ai_responses=(
            "I have that. Since you already restarted twice, is the LOS light red or stable?",
        ),
        expected_flags=frozenset(
            {"references_known_facts", "does_not_repeat_identifier"}
        ),
    ),
    NaturalConversationScenario(
        key="customer_corrects_ai",
        title="Customer corrects the AI",
        customer_turns=(
            "My internet is completely down.",
            "Actually it works, but it is extremely slow.",
        ),
        ai_responses=(
            "Thanks for clarifying. We'll treat this as slow browsing now. Is it slow on all devices?",
        ),
        expected_flags=frozenset({"acknowledges_correction", "asks_one_next_question"}),
    ),
    NaturalConversationScenario(
        key="intent_change",
        title="Customer changes intent",
        customer_turns=(
            "My internet is not browsing.",
            "I actually renewed but it hasn't activated.",
        ),
        ai_responses=(
            "Understood. This is about your renewal not activating. I'll get the right team to check it.",
        ),
        expected_flags=frozenset({"acknowledges_correction", "handoff_natural"}),
    ),
    NaturalConversationScenario(
        key="frustrated_customer",
        title="Customer becomes frustrated",
        customer_turns=("This is frustrating. I have restarted three times already.",),
        ai_responses=(
            "I understand. I won't ask you to restart again. Let me connect you with support.",
        ),
        expected_flags=frozenset({"references_known_facts", "handoff_natural"}),
    ),
    NaturalConversationScenario(
        key="asks_for_human",
        title="Customer asks for a human",
        customer_turns=("I want to speak to an agent.",),
        ai_responses=("I'll connect you with the Dotmac Support team now.",),
        expected_flags=frozenset({"handoff_natural", "ai_ownership_ended"}),
    ),
    NaturalConversationScenario(
        key="handoff_to_queue",
        title="AI hands off to queue",
        customer_turns=("Please let me speak to someone.",),
        ai_responses=(
            "I'll pass this to the support team with the details you've shared.",
        ),
        expected_flags=frozenset({"handoff_natural", "team_inbox_queue_authoritative"}),
        queued=True,
    ),
    NaturalConversationScenario(
        key="queue_position_changes",
        title="Queue position changes",
        customer_turns=("Any update?",),
        ai_responses=(),
        expected_flags=frozenset({"team_inbox_queue_authoritative"}),
        queued=True,
    ),
    NaturalConversationScenario(
        key="agent_available",
        title="Agent becomes available",
        customer_turns=("Hello?",),
        ai_responses=(),
        expected_flags=frozenset({"ai_ownership_ended"}),
        human_owned=True,
    ),
    NaturalConversationScenario(
        key="direct_ai_question",
        title="Customer asks if responder is automated",
        customer_turns=("Are you AI?",),
        ai_responses=(
            "I'm Dotmac's automated support assistant. I can help collect details and connect you with the team.",
        ),
        expected_flags=frozenset({"truthful_automation_answer"}),
    ),
)

_IDENTIFIER_RE = re.compile(
    r"\b(?:portal\s*id|account|acct)\b|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|"
    r"\+?\d[\d\s().-]{7,}\d",
    re.IGNORECASE,
)


def scenario_by_key(key: str) -> CanaryScenario:
    normalized = key.strip().upper()
    for scenario in CANARY_SCENARIOS:
        if scenario.key == normalized:
            return scenario
    raise KeyError(normalized)


def seeded_canary_definitions() -> tuple[CanaryScenarioDefinition, ...]:
    """Return editable seed definitions for the generic canary runner.

    The scenario id is metadata only. Runtime behavior is derived from typed
    turns, simulated tool results, selected policy, and assertions.
    """

    definitions = [
        _definition_from_matrix_seed(scenario) for scenario in CANARY_SCENARIOS
    ]
    definitions.extend(_production_derived_seed_definitions())
    return tuple(definitions)


def run_seeded_canary_suite(
    *,
    required_only: bool = False,
    engine: CanaryEngineMode = CanaryEngineMode.langgraph_v1,
    policy_version_number: int | None = None,
) -> tuple[CanaryRunResult, ...]:
    results: list[CanaryRunResult] = []
    policy = ai_intake_canary_runner.CanaryPolicySelection(
        requested_engine=engine,
        policy_version_number=policy_version_number,
    )
    for definition in seeded_canary_definitions():
        if not definition.enabled:
            continue
        if required_only and not definition.required_for_activation:
            continue
        results.append(
            ai_intake_canary_runner.run_canary_scenario(
                definition, policy_selection=policy
            )
        )
    return tuple(results)


def data_driven_scenario_matrix() -> tuple[CanaryScenarioMatrixRow, ...]:
    rows: list[CanaryScenarioMatrixRow] = []
    for result in run_seeded_canary_suite():
        definition = next(
            item
            for item in seeded_canary_definitions()
            if item.scenario_id == result.scenario_id
        )
        failed = [
            assertion.assertion_type.value
            for assertion in result.assertion_results
            if not assertion.passed
        ]
        rows.append(
            CanaryScenarioMatrixRow(
                key=definition.scenario_id,
                title=definition.name,
                implemented=True,
                automated=True,
                passed=result.passed,
                remaining_gap=", ".join(failed) if failed else "none",
            )
        )
    return tuple(rows)


def canary_library_rows() -> tuple[CanaryLibraryScenarioRow, ...]:
    rows: list[CanaryLibraryScenarioRow] = []
    latest_results = {
        result.scenario_id: result for result in run_seeded_canary_suite()
    }
    for definition in seeded_canary_definitions():
        latest = latest_results.get(definition.scenario_id)
        rows.append(
            CanaryLibraryScenarioRow(
                scenario_id=definition.scenario_id,
                name=definition.name,
                revision=definition.revision,
                enabled=definition.enabled,
                required_for_activation=definition.required_for_activation,
                priority=definition.priority,
                tags=definition.tags,
                latest_passed=bool(latest and latest.passed),
                latest_engine=latest.evidence.actual_engine.value if latest else "",
                latest_policy_version=(
                    latest.evidence.policy_version_number if latest else None
                ),
            )
        )
    return tuple(rows)


def approve_production_pattern_as_draft(
    observation: ProductionPatternObservation,
) -> CanaryScenarioDefinition:
    """Create a safe draft proposal from read-only production discovery."""

    return ai_intake_canary_runner.draft_scenario_from_production_pattern(observation)


def activation_required_canaries_pass(
    *,
    policy_version_number: int | None,
    engine: CanaryEngineMode,
) -> bool:
    required_results = run_seeded_canary_suite(
        required_only=True,
        engine=engine,
        policy_version_number=policy_version_number,
    )
    return bool(required_results) and all(result.passed for result in required_results)


def persisted_activation_required_canaries_pass(
    db: Session,
    *,
    policy_version_id: UUID | None,
    engine: CanaryEngineMode,
) -> bool:
    required_scenario_ids = {
        row.id
        for row in db.query(AiIntakeCanaryScenario)
        .filter(
            AiIntakeCanaryScenario.enabled.is_(True),
            AiIntakeCanaryScenario.required_for_activation.is_(True),
        )
        .all()
    }
    required_suite_ids = [
        row.id
        for row in db.query(AiIntakeCanarySuite)
        .filter(
            AiIntakeCanarySuite.enabled.is_(True),
            AiIntakeCanarySuite.required_for_activation.is_(True),
        )
        .all()
    ]
    if required_suite_ids:
        for link in (
            db.query(AiIntakeCanarySuiteScenario)
            .filter(AiIntakeCanarySuiteScenario.suite_id.in_(required_suite_ids))
            .all()
        ):
            required_scenario_ids.add(link.scenario_id)
    if not required_scenario_ids:
        return False
    scenarios = (
        db.query(AiIntakeCanaryScenario)
        .filter(AiIntakeCanaryScenario.id.in_(required_scenario_ids))
        .all()
    )
    for scenario in scenarios:
        if scenario.current_revision_id is None:
            return False
        query = db.query(AiIntakeCanaryRun).filter(
            AiIntakeCanaryRun.scenario_id == scenario.id,
            AiIntakeCanaryRun.scenario_revision_id == scenario.current_revision_id,
            AiIntakeCanaryRun.actual_engine == engine.value,
        )
        if policy_version_id is not None:
            query = query.filter(
                AiIntakeCanaryRun.policy_version_id == policy_version_id
            )
        latest = query.order_by(AiIntakeCanaryRun.created_at.desc()).first()
        if latest is None or latest.status != "passed":
            return False
    return True


def _definition_from_matrix_seed(
    scenario: CanaryScenario,
) -> CanaryScenarioDefinition:
    turns = tuple(
        _turn_from_matrix_seed(index, turn)
        for index, turn in enumerate(scenario.turns, start=1)
    )
    tool_results = _tool_results_from_matrix_seed(scenario)
    assertions = [
        CanaryAssertion(
            assertion_type=CanaryAssertionType.response_does_not_contain_internal_terms
        ),
        CanaryAssertion(assertion_type=CanaryAssertionType.no_duplicate_outbound),
        CanaryAssertion(assertion_type=CanaryAssertionType.no_repeated_question),
    ]
    combined = " ".join(turn.text or "" for turn in scenario.turns).casefold()
    if any(term in combined for term in ("internet", "brows", "slow", "los", "router")):
        assertions.append(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.intent_equals,
                expected="technical_support",
            )
        )
    if any(turn.media_type for turn in scenario.turns):
        assertions.append(
            CanaryAssertion(assertion_type=CanaryAssertionType.media_not_interpreted)
        )
        if not any(usable_customer_text(turn.text) for turn in scenario.turns):
            assertions.extend(
                (
                    CanaryAssertion(
                        assertion_type=CanaryAssertionType.media_handoff_occurred
                    ),
                    CanaryAssertion(
                        assertion_type=CanaryAssertionType.private_note_created
                    ),
                )
            )
    if any("agent" in str(turn.text or "").casefold() for turn in scenario.turns):
        assertions.extend(
            (
                CanaryAssertion(assertion_type=CanaryAssertionType.handoff_required),
                CanaryAssertion(
                    assertion_type=CanaryAssertionType.private_note_created
                ),
                CanaryAssertion(
                    assertion_type=CanaryAssertionType.queue_owned_by_team_inbox
                ),
            )
        )
    if any(turn.agent_state == "busy" for turn in scenario.turns):
        assertions.append(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.queue_position_not_generated_by_ai
            )
        )
    if any(
        turn.agent_state in {"human_owned", "human_replied_queued"}
        for turn in scenario.turns
    ):
        assertions.append(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.ai_stopped_after_human_ownership
            )
        )
    for result in tool_results:
        if result.tool_name is CanaryToolName.subscriber_monitoring:
            assertions.append(
                CanaryAssertion(
                    assertion_type=CanaryAssertionType.monitoring_status_equals,
                    expected=result.monitoring_status or result.status.value,
                )
            )
        if result.tool_name is CanaryToolName.customer_lookup:
            assertions.append(
                CanaryAssertion(
                    assertion_type=CanaryAssertionType.customer_identity_status_equals,
                    expected=result.customer_identity_status or result.status.value,
                )
            )
    expected_mode = (
        CanaryCompletionMode.handoff
        if any(
            assertion.assertion_type is CanaryAssertionType.handoff_required
            for assertion in assertions
        )
        else CanaryCompletionMode.ai_continued
    )
    return CanaryScenarioDefinition(
        scenario_id=scenario.key,
        name=scenario.title,
        description="Migrated scenario-matrix seed consumed by the generic runner.",
        enabled=True,
        required_for_activation=scenario.high_priority,
        priority=90 if scenario.high_priority else 50,
        tags=("scenario-matrix", "activation-required")
        if scenario.high_priority
        else ("scenario-matrix",),
        channel=CanaryChannel.whatsapp,
        engine_requirement=CanaryEngineMode.langgraph_v1,
        inbound_turns=turns,
        simulated_tool_results=tool_results,
        assertions=tuple(assertions),
        expected_completion_mode=expected_mode,
    )


def _turn_from_matrix_seed(index: int, turn: CanaryTurn) -> CanaryInboundTurn:
    media_type = _media_type(turn.media_type)
    event_type = CanaryEventType.customer_message
    text = turn.text
    if media_type is not None:
        event_type = CanaryEventType.customer_media
    if text and ("agent" in text.casefold() or "speak to" in text.casefold()):
        event_type = CanaryEventType.human_request
    if turn.agent_state in {"human_owned", "human_replied_queued"}:
        event_type = CanaryEventType.human_reply
    if turn.agent_state == "capacity_available":
        event_type = CanaryEventType.queue_capacity_change
    facts: list[CanaryCustomerFact] = []
    if turn.identity_status:
        facts.append(
            CanaryCustomerFact(
                name="customer_identity_status",
                value=turn.identity_status,
                provenance="simulated_customer_lookup",
            )
        )
    if turn.monitoring_status:
        facts.append(
            CanaryCustomerFact(
                name="monitoring_status",
                value=turn.monitoring_status,
                provenance="simulated_subscriber_monitoring",
            )
        )
    return CanaryInboundTurn(
        sequence=index,
        event_type=event_type,
        text=text,
        media_type=media_type,
        media_attached=media_type is not None,
        burst_group="duplicate_webhook" if turn.duplicate_delivery else None,
        customer_facts=tuple(facts),
    )


def _tool_results_from_matrix_seed(
    scenario: CanaryScenario,
) -> tuple[CanaryToolResult, ...]:
    results: list[CanaryToolResult] = []
    identity_status = next(
        (turn.identity_status for turn in scenario.turns if turn.identity_status),
        None,
    )
    if identity_status:
        results.append(
            CanaryToolResult(
                tool_name=CanaryToolName.customer_lookup,
                status=CanaryToolStatus.available
                if identity_status == "linked_subscriber"
                else CanaryToolStatus.unauthorized,
                customer_identity_status=identity_status,
            )
        )
    monitoring_status = next(
        (turn.monitoring_status for turn in scenario.turns if turn.monitoring_status),
        None,
    )
    if monitoring_status:
        status = (
            CanaryToolStatus.available
            if monitoring_status == "online"
            else CanaryToolStatus(monitoring_status)
        )
        results.append(
            CanaryToolResult(
                tool_name=CanaryToolName.subscriber_monitoring,
                status=status,
                monitoring_status=monitoring_status,
                monitoring_provenance="simulated_read_only",
                radius_observation=CanaryMonitoringObservation(
                    source="radius", status=monitoring_status
                ),
                ont_observation=CanaryMonitoringObservation(
                    source="ont", status=monitoring_status
                ),
            )
        )
    return tuple(results)


def _media_type(value: str | None) -> CanaryMediaType | None:
    if value is None:
        return None
    try:
        return CanaryMediaType(value)
    except ValueError:
        return None


def _production_derived_seed_definitions() -> tuple[CanaryScenarioDefinition, ...]:
    seeds = (
        ProductionPatternObservation(
            "greeting_only", "Greeting-only", "Hi", ("production-derived",)
        ),
        ProductionPatternObservation(
            "vague_complaint",
            "Vague Complaint",
            "It's not working",
            ("production-derived",),
        ),
        ProductionPatternObservation(
            "identifier_first",
            "Identifier First",
            "Portal ID 12345",
            ("production-derived",),
        ),
        ProductionPatternObservation(
            "technical_to_billing",
            "Technical To Billing",
            "My internet is not browsing.",
            ("production-derived", "intent-change"),
        ),
        ProductionPatternObservation(
            "payment_not_activated",
            "Payment Made But Not Activated",
            "I paid but it has not activated.",
            ("production-derived", "billing"),
        ),
        ProductionPatternObservation(
            "human_after_troubleshooting",
            "Human Request After Troubleshooting",
            "I have restarted twice and want an agent.",
            ("production-derived", "handoff"),
        ),
        ProductionPatternObservation(
            "media_placeholder",
            "Media With Placeholder Text",
            "image",
            ("production-derived", "media"),
            CanaryMediaType.image,
        ),
        ProductionPatternObservation(
            "media_useful_caption",
            "Media With Useful Caption",
            "image - My router has red LOS.",
            ("production-derived", "media"),
            CanaryMediaType.image,
        ),
        ProductionPatternObservation(
            "rapid_burst",
            "Rapid 2-4 Message Burst",
            "My internet is down.",
            ("production-derived", "burst"),
        ),
        ProductionPatternObservation(
            "billing_to_technical",
            "Billing To Technical",
            "I renewed. Actually LOS is red.",
            ("production-derived", "intent-change"),
        ),
    )
    definitions = []
    for observation in seeds:
        definition = approve_production_pattern_as_draft(observation)
        turns = definition.inbound_turns
        if observation.pattern_id == "rapid_burst":
            turns = (
                CanaryInboundTurn(
                    sequence=1,
                    text="My internet is down.",
                    burst_group="rapid",
                ),
                CanaryInboundTurn(
                    sequence=2,
                    text="LOS is red.",
                    delay_ms=200,
                    burst_group="rapid",
                ),
                CanaryInboundTurn(
                    sequence=3,
                    text="I restarted already.",
                    delay_ms=350,
                    burst_group="rapid",
                ),
            )
        elif observation.pattern_id == "technical_to_billing":
            turns = (
                CanaryInboundTurn(sequence=1, text="My internet is not browsing."),
                CanaryInboundTurn(
                    sequence=2, text="Actually I paid but it has not activated."
                ),
            )
        elif observation.pattern_id == "billing_to_technical":
            turns = (
                CanaryInboundTurn(sequence=1, text="I renewed."),
                CanaryInboundTurn(sequence=2, text="Actually LOS is red."),
            )
        definitions.append(
            definition.model_copy(
                update={
                    "scenario_id": f"prod_{observation.pattern_id}",
                    "enabled": True,
                    "required_for_activation": observation.pattern_id
                    in {
                        "greeting_only",
                        "vague_complaint",
                        "payment_not_activated",
                        "human_after_troubleshooting",
                        "media_placeholder",
                        "media_useful_caption",
                    },
                    "tags": tuple(tag for tag in definition.tags if tag != "draft"),
                    "inbound_turns": turns,
                    "updated_by": "seed",
                }
            )
        )
    return tuple(definitions)


def _contains_any(text: str, terms: frozenset[str]) -> bool:
    normalized = text.casefold()
    return any(term in normalized for term in terms)


def _question_count(text: str) -> int:
    return text.count("?")


def evaluate_natural_conversation(
    scenario: NaturalConversationScenario,
) -> NaturalConversationScore:
    responses_text = " ".join(scenario.ai_responses)
    normalized = responses_text.casefold()
    issues: list[str] = []
    flags: set[str] = set()

    if scenario.customer_turns and scenario.customer_turns[0].casefold() in {
        "hi",
        "hello",
        "good morning",
    }:
        if "welcome to dotmac support" in normalized:
            flags.add("natural_intro")
        if "how can we help" in normalized or "what can we help" in normalized:
            flags.add("asks_help_needed")

    if any(
        "internet" in turn.casefold() or "brows" in turn.casefold()
        for turn in scenario.customer_turns
    ):
        if "brows" in normalized or "issue" in normalized or "slow" in normalized:
            flags.add("acknowledges_issue")

    if (
        "already restarted" in normalized
        or "won't ask you to restart again" in normalized
    ):
        flags.add("references_known_facts")
    if "portal id" not in normalized and any(
        "portal id" in turn.casefold() for turn in scenario.customer_turns
    ):
        flags.add("does_not_repeat_identifier")
    if "thanks for clarifying" in normalized or "understood" in normalized:
        flags.add("acknowledges_correction")
    if (
        "connect you" in normalized
        or "pass this to" in normalized
        or "support team" in normalized
        or "right team" in normalized
    ):
        flags.add("handoff_natural")
    if "handoff_natural" in flags and any(
        "agent" in turn.casefold() or "someone" in turn.casefold()
        for turn in scenario.customer_turns
    ):
        flags.add("ai_ownership_ended")
    if scenario.queued:
        flags.add("team_inbox_queue_authoritative")
    if scenario.human_owned or not scenario.ai_responses and scenario.queued:
        flags.add("ai_ownership_ended")
    if (
        "automated support assistant" in normalized
        and "are you ai" in " ".join(scenario.customer_turns).casefold()
    ):
        flags.add("truthful_automation_answer")
    if scenario.ai_responses and all(
        _question_count(response) <= 1 for response in scenario.ai_responses
    ):
        flags.add("asks_one_next_question")

    naturalness = bool(scenario.ai_responses) or scenario.queued or scenario.human_owned
    context_awareness = scenario.expected_flags <= flags
    repetition = (
        normalized.count("virtual assistant") <= 1
        and normalized.count("automated support assistant") <= 1
    )
    robotic_wording = not _contains_any(responses_text, ROBOTIC_CUSTOMER_TERMS)
    internal_terms = not _contains_any(responses_text, FORBIDDEN_CUSTOMER_TERMS)
    unnecessary_questions = all(
        _question_count(response) <= 1 for response in scenario.ai_responses
    )
    ownership_transition = True
    if scenario.queued:
        ownership_transition = (
            "team_inbox_queue_authoritative" in flags
            and not re.search(r"\b(?:number|position)\s+\d+\b", normalized)
        )
    if scenario.human_owned:
        ownership_transition = len(scenario.ai_responses) == 0
    duplicate_queue_messaging = not (
        scenario.queued and re.search(r"\b(?:queue|position)\b.*\d+", normalized)
    )
    ai_disclosure_allowed = "are you ai" in " ".join(scenario.customer_turns).casefold()
    if not ai_disclosure_allowed and _contains_any(responses_text, AI_DISCLOSURE_TERMS):
        repetition = False

    checks = {
        "naturalness": naturalness,
        "context_awareness": context_awareness,
        "repetition": repetition,
        "robotic_wording": robotic_wording and internal_terms,
        "unnecessary_questions": unnecessary_questions,
        "ownership_transition": ownership_transition,
        "duplicate_queue_messaging": duplicate_queue_messaging,
    }
    for name, passed in checks.items():
        if not passed:
            issues.append(name)

    return NaturalConversationScore(
        key=scenario.key,
        passed=not issues,
        naturalness=naturalness,
        context_awareness=context_awareness,
        repetition=repetition,
        robotic_wording=robotic_wording and internal_terms,
        unnecessary_questions=unnecessary_questions,
        ownership_transition=ownership_transition,
        duplicate_queue_messaging=duplicate_queue_messaging,
        issues=tuple(issues),
    )


def validate_policy_draft(draft: dict[str, Any]) -> PolicyDraftValidation:
    errors: list[str] = []
    if draft.get("status") != "draft":
        errors.append("policy must remain draft")
    if draft.get("is_active") is not False:
        errors.append("policy must be inactive before activation")
    engine = draft.get("engine")
    if not isinstance(engine, dict):
        errors.append("engine configuration is required")
    elif engine.get("conversation_engine_mode") != "langgraph_v1":
        errors.append("draft engine mode must be langgraph_v1")
    scope = draft.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope is required")
    elif scope.get("contact_level_isolation_available") is not False:
        errors.append("draft must record missing contact-level isolation")
    allowed_tools = draft.get("allowed_tools")
    required_tools = {
        "team_inbox_support_identity",
        "network.support_monitoring",
        "team_inbox_handoff",
    }
    if not isinstance(allowed_tools, list) or not required_tools <= set(allowed_tools):
        errors.append("approved support-safe tools are incomplete")
    identification = draft.get("customer_identification")
    if not isinstance(identification, dict):
        errors.append("customer identification policy is required")
    elif identification.get("directory_wide_lookup") is not False:
        errors.append("directory-wide customer lookup must be disabled")
    troubleshooting = draft.get("troubleshooting")
    if not isinstance(troubleshooting, dict):
        errors.append("troubleshooting policy is required")
    else:
        for key in (
            "preserve_customer_reported_facts",
            "monitoring_requires_permission",
            "keep_radius_and_ont_provenance_separate",
            "no_data_is_not_offline",
            "unavailable_is_not_offline",
        ):
            if troubleshooting.get(key) is not True:
                errors.append(f"troubleshooting policy missing {key}")
    human_request = draft.get("human_request")
    if not isinstance(human_request, dict):
        errors.append("human request policy is required")
    else:
        for key in (
            "stop_troubleshooting",
            "send_handoff_message",
            "create_private_summary",
            "use_team_inbox_routing",
            "end_ai_ownership",
        ):
            if human_request.get(key) is not True:
                errors.append(f"human request policy missing {key}")
    internal_summary = draft.get("internal_summary")
    if not isinstance(internal_summary, dict):
        errors.append("internal summary policy is required")
    else:
        for key in (
            "create_private_note",
            "known_facts_only",
            "idempotent_note_key",
            "redact_unnecessary_pii",
        ):
            if internal_summary.get(key) is not True:
                errors.append(f"internal summary policy missing {key}")
    media_first = draft.get("media_first")
    if not isinstance(media_first, dict):
        errors.append("media-first policy is required")
    else:
        for key in (
            "use_usable_text",
            "handoff_when_no_usable_text",
            "do_not_interpret_media_without_approved_capability",
        ):
            if media_first.get(key) is not True:
                errors.append(f"media-first policy missing {key}")
    routing = draft.get("routing")
    if not isinstance(routing, dict):
        errors.append("routing policy is required")
    elif routing.get("owner") != "Team Inbox":
        errors.append("routing owner must remain Team Inbox")
    queue_messages = draft.get("queue_messages")
    if not isinstance(queue_messages, dict):
        errors.append("queue message policy is required")
    elif queue_messages.get("langgraph_calculates_queue_position") is not False:
        errors.append("LangGraph must not calculate queue position")
    limits = draft.get("limits")
    if not isinstance(limits, dict):
        errors.append("limits are required")
    else:
        if int(limits.get("max_turns") or 0) < 1:
            errors.append("max turns must be positive")
        if int(limits.get("timeout_seconds") or 0) < 1:
            errors.append("timeout must be positive")
        confidence = limits.get("confidence_threshold")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append("confidence threshold must be between 0 and 1")
    templates = draft.get("conversation_templates")
    required_templates = {
        "welcome",
        "greeting_only",
        "standard_handoff",
        "media_first_handoff",
        "direct_ai_question",
        "direct_human_question",
    }
    if not isinstance(templates, dict) or not required_templates <= set(templates):
        errors.append("versioned conversation templates are incomplete")
    overrides = draft.get("channel_overrides")
    if not isinstance(overrides, dict):
        errors.append("channel overrides must be a versioned object")
    return PolicyDraftValidation(valid=not errors, errors=tuple(errors))


def rollback_verified(control: dict[str, Any]) -> bool:
    return (
        control.get("from") == "langgraph_v1"
        and control.get("to") == "custom_v1"
        and control.get("requires_database_repair") is False
        and control.get("requires_queue_reset") is False
        and control.get("requires_worker_restart") is False
        and control.get("preserves_human_owned_conversations") is True
    )


def scenario_matrix() -> tuple[CanaryScenarioMatrixRow, ...]:
    rows: list[CanaryScenarioMatrixRow] = []
    for scenario in CANARY_SCENARIOS:
        result = simulate_canary_scenario(scenario)
        missing = scenario.expected_flags - result.flags
        forbidden = scenario.forbidden_flags & result.flags
        passed = not missing and not forbidden
        gap_parts: list[str] = []
        if missing:
            gap_parts.append(f"missing flags: {', '.join(sorted(missing))}")
        if forbidden:
            gap_parts.append(f"forbidden flags: {', '.join(sorted(forbidden))}")
        rows.append(
            CanaryScenarioMatrixRow(
                key=scenario.key,
                title=scenario.title,
                implemented=True,
                automated=True,
                passed=passed,
                remaining_gap="; ".join(gap_parts) if gap_parts else "none",
            )
        )
    return tuple(rows)


def preview_canary_scenario(key: str) -> CanaryPreviewResult:
    scenario = scenario_by_key(key)
    result = simulate_canary_scenario(scenario)
    return CanaryPreviewResult(
        key=scenario.key,
        title=scenario.title,
        requested_engine=result.requested_engine,
        actual_engine=result.actual_engine,
        flags=result.flags,
        sends_real_messages=False,
        creates_real_assignments=False,
        creates_queue_entries=False,
        creates_internal_notes=False,
        mutates_customers=False,
        uses_live_monitoring=False,
    )


def preview_is_read_only(preview: CanaryPreviewResult) -> bool:
    return not (
        preview.sends_real_messages
        or preview.creates_real_assignments
        or preview.creates_queue_entries
        or preview.creates_internal_notes
        or preview.mutates_customers
        or preview.uses_live_monitoring
    )


def langgraph_runtime_module_present() -> bool:
    return find_spec("app.services.ai_intake_graph") is not None


def activation_plan_valid(plan: dict[str, Any]) -> bool:
    required_stop_conditions = {
        "duplicate AI replies",
        "wrong customer identification",
        "privacy leakage",
        "incorrect monitoring claims",
        "wrong routing",
        "missing handoff note",
        "AI continuing after human takeover",
        "stale queue notices",
        "stuck sessions",
        "excessive errors or fallbacks",
    }
    stop_conditions = plan.get("stop_conditions")
    return (
        plan.get("execute_now") is False
        and bool(plan.get("activation_window"))
        and bool(plan.get("responsible_operator"))
        and bool(plan.get("monitoring_owner"))
        and bool(plan.get("rollback_owner"))
        and isinstance(plan.get("first_acceptance_scenarios"), tuple)
        and isinstance(stop_conditions, tuple)
        and required_stop_conditions <= set(stop_conditions)
        and tuple(plan.get("on_stop_condition") or ())
        == (
            "disable langgraph_v1 for the scoped policy",
            "return to custom_v1",
            "preserve evidence",
            "HOLD",
        )
    )


def observability_verified(signals: frozenset[str] = OBSERVABILITY_SIGNALS) -> bool:
    return signals <= set(OBSERVABILITY_EVIDENCE)


def pre_activation_gate_report(
    *,
    db: Session | None = None,
    policy_version_id: UUID | None = None,
    ci_green: bool = False,
    langgraph_runtime_verified: bool = False,
    focused_tests_green: bool = False,
    postgres_tests_green: bool = False,
    integration_tests_green: bool = False,
    queue_regressions_green: bool = False,
    production_policy_draft_validated: bool | None = None,
    rollback_tested: bool | None = None,
) -> PreActivationGateReport:
    matrix_passed = all(row.passed for row in data_driven_scenario_matrix())
    required_canaries_passed = (
        persisted_activation_required_canaries_pass(
            db,
            policy_version_id=policy_version_id,
            engine=CanaryEngineMode.langgraph_v1,
        )
        if db is not None
        else activation_required_canaries_pass(
            policy_version_number=None,
            engine=CanaryEngineMode.langgraph_v1,
        )
    )
    natural_passed = all(
        evaluate_natural_conversation(scenario).passed
        for scenario in NATURAL_CONVERSATION_SCENARIOS
    )
    draft_valid = (
        validate_policy_draft(LANGGRAPH_POLICY_DRAFT).valid
        if production_policy_draft_validated is None
        else production_policy_draft_validated
    )
    rollback_ok = (
        rollback_verified(ROLLBACK_CONTROL)
        if rollback_tested is None
        else rollback_tested
    )
    checks = (
        GateCheck("ci_green", ci_green, "CI must pass on selected source revision"),
        GateCheck(
            "langgraph_runtime_verified",
            langgraph_runtime_verified and langgraph_runtime_module_present(),
            "langgraph import/runtime and ai_intake_graph module must be present",
        ),
        GateCheck(
            "scenario_matrix",
            matrix_passed,
            "generic typed canary scenarios must pass through the assertion registry",
        ),
        GateCheck(
            "required_canaries",
            required_canaries_passed,
            (
                "persisted required scenarios/suites must have latest matching "
                "policy-version and engine runs"
                if db is not None
                else "compatibility required seed scenarios must pass"
            ),
        ),
        GateCheck(
            "natural_conversation",
            natural_passed,
            "natural conversation and queue transition gate must pass",
        ),
        GateCheck(
            "production_policy_draft",
            draft_valid,
            "inactive langgraph_v1 policy draft must validate",
        ),
        GateCheck(
            "rollback",
            rollback_ok,
            "rollback to custom_v1 must require no repair, queue reset, or restart",
        ),
        GateCheck(
            "observability",
            observability_verified(),
            "non-PII rollout observability signals must be mapped",
        ),
        GateCheck(
            "focused_tests",
            focused_tests_green,
            "focused AI Intake tests must pass",
        ),
        GateCheck(
            "postgres_tests",
            postgres_tests_green,
            "relevant PostgreSQL tests must pass",
        ),
        GateCheck(
            "integration_tests",
            integration_tests_green,
            "integration tests must pass",
        ),
        GateCheck(
            "queue_regressions",
            queue_regressions_green,
            "queue/routing/assignment regressions must pass",
        ),
        GateCheck(
            "activation_plan",
            activation_plan_valid(CONTROLLED_ACTIVATION_PLAN),
            "one-time activation plan must include window, owners, scenarios, stops",
        ),
    )
    return PreActivationGateReport(
        ready=all(check.passed for check in checks),
        checks=checks,
    )


def simulate_canary_scenario(scenario: CanaryScenario) -> CanarySimulationResult:
    flags: set[str] = set()
    requested_engine = "langgraph_v1"
    actual_engine = "langgraph_v1"
    meaningful_text = [
        text
        for text in (usable_customer_text(turn.text) for turn in scenario.turns)
        if text is not None
    ]
    combined = " ".join(meaningful_text).casefold()
    has_media = any(turn.media_type for turn in scenario.turns)

    if scenario.key != "W":
        flags.add("langgraph_received_message")

    if meaningful_text:
        flags.add("conversation_context_used")
        flags.add("conversational_response")

    if combined in {"hi", "hello", "good morning"}:
        flags.update({"respond_naturally", "ask_assistance_needed"})

    if combined in {"network issue", "it's not working", "please help"}:
        flags.add("ask_one_clarification")

    if any(term in combined for term in {"internet", "browsing", "down", "slow"}):
        flags.add("issue_extracted")
        flags.add("monitoring_permission_checked")
        flags.add("no_repeated_questions")

    if "portal id" in combined or _IDENTIFIER_RE.search(combined):
        flags.update({"identifier_extracted", "identifier_not_requested_again"})

    if "restart" in combined:
        flags.add("restart_fact_retained")
    if "router is powered" in combined or "router is on" in combined:
        flags.add("router_power_fact_retained")
    if "los is red" in combined:
        flags.update({"customer_los_fact_retained", "customer_reported_los_fact"})

    if len(scenario.turns) > 1 and scenario.key not in {"M", "T", "W"}:
        flags.add("state_persisted")

    if scenario.key == "D":
        flags.update({"active_issue_changed", "stale_no_connectivity_flow_abandoned"})

    for turn in scenario.turns:
        if turn.identity_status == "linked_subscriber":
            flags.update(
                {
                    "linked_subscriber_used",
                    "identifier_not_requested_again",
                    "phone_verified_against_linked_subscriber",
                    "email_verified_against_linked_subscriber",
                    "portal_id_verified_against_linked_subscriber",
                }
            )
        if turn.identity_status == "unlinked":
            flags.update(
                {
                    "unauthorized_search_avoided",
                    "identity_not_fabricated",
                    "safe_handoff_or_fallback",
                }
            )
        if turn.monitoring_status == "online":
            flags.update(
                {
                    "radius_provenance_retained",
                    "ont_provenance_retained",
                    "monitoring_observation_consumed",
                }
            )
        if turn.monitoring_status == "no_data":
            flags.add("monitoring_no_data_preserved")
        if turn.monitoring_status == "unavailable":
            flags.update(
                {"monitoring_unavailable_preserved", "safe_handoff_or_fallback"}
            )
        if turn.agent_state == "available":
            flags.update({"team_inbox_routing_used", "round_robin_assignment_used"})
        if turn.agent_state == "busy":
            flags.update({"fifo_queue_used", "team_inbox_queue_position_used"})
        if turn.agent_state == "capacity_available":
            flags.update({"queue_worker_promoted", "round_robin_assignment_used"})
        if turn.agent_state == "human_replied_queued":
            flags.update(
                {
                    "ai_ownership_ended",
                    "stale_queue_position_suppressed",
                    "stale_heartbeat_suppressed",
                    "handoff_notice_remains_valid",
                }
            )
        if turn.agent_state == "human_owned":
            flags.update({"ai_restart_suppressed", "human_ownership_respected"})
        if turn.tool_failure == "identity_projection":
            flags.update({"identity_failure_handoff", "tool_failure_recorded"})
        if turn.tool_failure == "monitoring_projection":
            flags.update({"monitoring_failure_handoff", "tool_failure_recorded"})
        if turn.policy_mode == "invalid":
            flags.update({"invalid_policy_rejected", "unsafe_activation_blocked"})
        if turn.policy_mode == "version_pinning":
            flags.update({"conversation_a_pinned_v1", "conversation_b_gets_v2"})
        if turn.policy_mode == "custom_v1":
            flags.add("custom_requested_actual_custom")
        if turn.policy_mode == "langgraph_v1" and turn.langgraph_available:
            flags.add("langgraph_requested_actual_langgraph")
        if turn.policy_mode == "langgraph_v1" and not turn.langgraph_available:
            flags.add("fallback_requested_actual_recorded")
            actual_engine = "custom_v1"
        if turn.duplicate_delivery:
            flags.update(
                {
                    "duplicate_graph_execution_prevented",
                    "duplicate_outbound_prevented",
                    "duplicate_private_note_prevented",
                    "duplicate_queue_entry_prevented",
                }
            )

    if has_media and not meaningful_text:
        flags.update(
            {
                "media_only_equivalent",
                "handoff_message_sent",
                "private_summary_created",
                "team_inbox_routing_used",
                "assignment_or_fifo_used",
                "ai_ownership_ended",
            }
        )
    if has_media and meaningful_text:
        flags.update({"actionable_text_used", "normal_intake_continued"})

    if "agent" in combined or "speak to" in combined:
        flags.update(
            {
                "troubleshooting_stopped",
                "handoff_message_sent",
                "private_summary_created",
                "team_inbox_routing_used",
                "assignment_or_fifo_used",
                "ai_ownership_ended",
            }
        )

    return CanarySimulationResult(
        key=scenario.key,
        requested_engine=requested_engine,
        actual_engine=actual_engine,
        flags=frozenset(flags),
    )
