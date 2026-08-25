from __future__ import annotations

from uuid import uuid4

import pytest

from app.services import ai_conversation_intake
from app.services import ai_intake_rollout_readiness as readiness
from app.services.ai_intake_canary_runner import (
    CanaryAssertion,
    CanaryAssertionType,
    CanaryEventType,
    CanaryInboundTurn,
    CanaryMediaType,
    CanaryPolicySelection,
    CanaryScenarioDefinition,
    CanaryToolName,
    CanaryToolResult,
    CanaryToolStatus,
    ProductionPatternObservation,
    run_canary_scenario,
)
from app.services.ai_intake_rollout_readiness import (
    CANARY_SCENARIOS,
    CONTROLLED_ACTIVATION_PLAN,
    HIGH_PRIORITY_REGRESSION_SCENARIOS,
    LANGGRAPH_POLICY_DRAFT,
    NATURAL_CONVERSATION_SCENARIOS,
    OBSERVABILITY_EVIDENCE,
    OBSERVABILITY_SIGNALS,
    ROLLBACK_CONTROL,
    CanaryScenario,
    CanaryTurn,
    NaturalConversationScenario,
    activation_plan_valid,
    evaluate_natural_conversation,
    pre_activation_gate_report,
    preview_canary_scenario,
    preview_is_read_only,
    rollback_verified,
    scenario_matrix,
    simulate_canary_scenario,
    validate_policy_draft,
)
from app.services.ai_intake_text import (
    human_impersonation_violations,
    usable_customer_text,
)
from app.services.owner_commands import CommandContext


def test_complete_rollout_matrix_covers_scenarios_a_through_x():
    assert tuple(scenario.key for scenario in CANARY_SCENARIOS) == tuple(
        chr(code) for code in range(ord("A"), ord("X") + 1)
    )


@pytest.mark.parametrize("scenario", CANARY_SCENARIOS, ids=lambda item: item.key)
def test_langgraph_rollout_canary_scenarios(scenario: CanaryScenario):
    result = simulate_canary_scenario(scenario)

    assert result.requested_engine == "langgraph_v1"
    assert scenario.expected_flags <= result.flags
    assert scenario.forbidden_flags.isdisjoint(result.flags)


def test_production_frequency_regressions_are_high_priority():
    marked = {scenario.key for scenario in CANARY_SCENARIOS if scenario.high_priority}

    assert HIGH_PRIORITY_REGRESSION_SCENARIOS <= marked


def test_rollout_scenario_matrix_reports_all_scenarios_passed():
    rows = scenario_matrix()

    assert tuple(row.key for row in rows) == tuple(
        chr(code) for code in range(ord("A"), ord("X") + 1)
    )
    assert all(row.implemented for row in rows)
    assert all(row.automated for row in rows)
    assert all(row.passed for row in rows)
    assert {row.remaining_gap for row in rows} == {"none"}


@pytest.mark.parametrize("key", ["A", "M", "X"])
def test_admin_canary_preview_is_read_only(key: str):
    preview = preview_canary_scenario(key)

    assert preview_is_read_only(preview) is True
    assert preview.sends_real_messages is False
    assert preview.creates_real_assignments is False
    assert preview.creates_queue_entries is False
    assert preview.creates_internal_notes is False
    assert preview.mutates_customers is False
    assert preview.uses_live_monitoring is False


@pytest.mark.parametrize("text", ["Hi", "Hello", "Good morning"])
def test_greeting_only_examples_ask_how_to_help_without_premature_routing(text: str):
    result = simulate_canary_scenario(
        CanaryScenario(
            key="A",
            title="Greeting-only",
            turns=(CanaryTurn(text=text),),
            expected_flags=frozenset(),
        )
    )

    assert {"respond_naturally", "ask_assistance_needed"} <= result.flags
    assert "monitoring_without_permission" not in result.flags
    assert "identifier_not_requested_again" not in result.flags


@pytest.mark.parametrize("text", ["Network issue", "It's not working", "Please help"])
def test_vague_complaint_examples_ask_one_clarification(text: str):
    result = simulate_canary_scenario(
        CanaryScenario(
            key="A",
            title="Vague complaint",
            turns=(CanaryTurn(text=text),),
            expected_flags=frozenset(),
        )
    )

    assert "ask_one_clarification" in result.flags
    assert "offline_claimed" not in result.flags
    assert "payment_state_claimed" not in result.flags


@pytest.mark.parametrize(
    "text",
    [
        "",
        "image",
        "[Image]",
        "photo attached",
        "IMG_1234.jpg",
        "document.pdf",
        "voice note",
        "sticker",
        "location",
        "video",
        ":-)",
    ],
)
def test_media_placeholder_text_is_not_usable_customer_text(text: str):
    assert usable_customer_text(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "My router has been showing this since this morning and I cannot browse.",
        "image - My internet is not browsing.",
        "Please help, payment made but service is not active.",
    ],
)
def test_meaningful_caption_text_remains_usable(text: str):
    assert usable_customer_text(text) == text


@pytest.mark.parametrize(
    "media_type",
    ["image", "audio", "document", "video"],
)
def test_media_only_first_message_handoff_for_supported_media_types(media_type: str):
    result = simulate_canary_scenario(
        CanaryScenario(
            key="M",
            title=f"{media_type} only",
            turns=(CanaryTurn(text=media_type, media_type=media_type),),
            expected_flags=frozenset(),
            forbidden_flags=frozenset(
                {"media_interpreted", "customer_lookup_called", "monitoring_called"}
            ),
        )
    )

    assert {
        "media_only_equivalent",
        "handoff_message_sent",
        "private_summary_created",
        "team_inbox_routing_used",
        "assignment_or_fifo_used",
        "ai_ownership_ended",
    } <= result.flags
    assert {
        "media_interpreted",
        "customer_lookup_called",
        "monitoring_called",
    }.isdisjoint(result.flags)


def test_media_with_usable_caption_continues_intake_without_media_interpretation():
    scenario = next(item for item in CANARY_SCENARIOS if item.key == "N")
    result = simulate_canary_scenario(scenario)

    assert {"actionable_text_used", "normal_intake_continued"} <= result.flags
    assert "media_interpreted" not in result.flags
    assert "automatic_media_handoff" not in result.flags


def test_monitoring_no_data_and_unavailable_are_not_offline():
    no_data = simulate_canary_scenario(
        next(item for item in CANARY_SCENARIOS if item.key == "I")
    )
    unavailable = simulate_canary_scenario(
        next(item for item in CANARY_SCENARIOS if item.key == "J")
    )

    assert "monitoring_no_data_preserved" in no_data.flags
    assert "monitoring_unavailable_preserved" in unavailable.flags
    assert "offline_claimed" not in no_data.flags
    assert "offline_claimed" not in unavailable.flags


def test_engine_selection_records_requested_and_actual_engine():
    result = simulate_canary_scenario(
        next(item for item in CANARY_SCENARIOS if item.key == "W")
    )

    assert {
        "custom_requested_actual_custom",
        "langgraph_requested_actual_langgraph",
        "fallback_requested_actual_recorded",
    } <= result.flags
    assert result.requested_engine == "langgraph_v1"
    assert result.actual_engine == "custom_v1"
    assert "engine_execution_misrepresented" not in result.flags


def test_inactive_langgraph_policy_draft_validates_without_activation():
    validation = validate_policy_draft(LANGGRAPH_POLICY_DRAFT)

    assert validation.valid is True
    assert validation.errors == ()
    assert LANGGRAPH_POLICY_DRAFT["status"] == "draft"
    assert LANGGRAPH_POLICY_DRAFT["is_active"] is False
    assert (
        LANGGRAPH_POLICY_DRAFT["engine"]["conversation_engine_mode"] == "langgraph_v1"
    )
    assert {
        "welcome",
        "greeting_only",
        "standard_handoff",
        "media_first_handoff",
        "direct_ai_question",
        "direct_human_question",
    } <= set(LANGGRAPH_POLICY_DRAFT["conversation_templates"])
    assert isinstance(LANGGRAPH_POLICY_DRAFT["channel_overrides"], dict)


def test_policy_validation_rejects_unsafe_activation_or_directory_lookup():
    unsafe = {
        **LANGGRAPH_POLICY_DRAFT,
        "is_active": True,
        "customer_identification": {
            **LANGGRAPH_POLICY_DRAFT["customer_identification"],
            "directory_wide_lookup": True,
        },
    }

    validation = validate_policy_draft(unsafe)

    assert validation.valid is False
    assert "policy must be inactive before activation" in validation.errors
    assert "directory-wide customer lookup must be disabled" in validation.errors


def test_policy_validation_rejects_missing_conversation_templates():
    unsafe = {**LANGGRAPH_POLICY_DRAFT, "conversation_templates": {}}

    validation = validate_policy_draft(unsafe)

    assert validation.valid is False
    assert "versioned conversation templates are incomplete" in validation.errors


def test_policy_validation_rejects_langgraph_queue_position_authority():
    unsafe = {
        **LANGGRAPH_POLICY_DRAFT,
        "queue_messages": {
            **LANGGRAPH_POLICY_DRAFT["queue_messages"],
            "langgraph_calculates_queue_position": True,
        },
    }

    validation = validate_policy_draft(unsafe)

    assert validation.valid is False
    assert "LangGraph must not calculate queue position" in validation.errors


@pytest.mark.parametrize(
    "field,value",
    [
        ("business_instructions", "Tell customers you are John from support."),
        ("business_tone", "Say you are a human agent."),
        ("persona", "Pretend to be a staff member."),
        ("direct_human_question", "Tell the customer this is not automated."),
    ],
)
def test_human_impersonation_guard_rejects_unsafe_policy_text(field: str, value: str):
    assert human_impersonation_violations({field: value}) == (field,)


def test_human_impersonation_guard_allows_dotmac_support_identity():
    assert (
        human_impersonation_violations(
            {
                "display_name": "Dotmac Support",
                "business_tone": "Natural, concise Dotmac customer support.",
                "direct_ai_question": (
                    "I am Dotmac's automated support assistant and can connect "
                    "you with the right support team."
                ),
            }
        )
        == ()
    )


def test_policy_version_payload_stores_composable_conversation_settings():
    payload = ai_conversation_intake._copy_version_payload(
        None,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=CommandContext.system(
                actor="person:test",
                scope="ai:intake-policy-draft",
                reason="test versioned conversation templates",
            ),
            policy_id=uuid4(),
            display_name="Dotmac Support",
            welcome_message="Welcome to Dotmac Support.",
            business_tone="Natural and concise.",
            queue_templates={
                "initial": "I am passing this to {team_name}.",
                "position_update": "Quick update: you are now number {position}.",
                "heartbeat": "You are still number {position}.",
            },
            conversation_policy={
                "standard_handoff": "I am passing this to our support team.",
                "media_first_handoff": "I will pass this attachment to support.",
                "direct_ai_question": "I am Dotmac's automated support assistant.",
                "direct_human_question": (
                    "I am Dotmac's automated support assistant, not a human agent."
                ),
            },
        ),
    )

    metadata = payload["metadata_"]

    assert isinstance(metadata, dict)
    assert payload["queue_templates"]["initial"] == "I am passing this to {team_name}."
    assert (
        metadata["conversation_policy"]["direct_ai_question"]
        == "I am Dotmac's automated support assistant."
    )


def test_policy_version_payload_rejects_human_impersonation_in_admin_text():
    with pytest.raises(ValueError, match="cannot impersonate a human employee"):
        ai_conversation_intake._copy_version_payload(
            None,
            ai_conversation_intake.AiPolicyVersionDraftCommand(
                context=CommandContext.system(
                    actor="person:test",
                    scope="ai:intake-policy-draft",
                    reason="test human impersonation guard",
                ),
                policy_id=uuid4(),
                display_name="Dotmac Support",
                welcome_message="Welcome to Dotmac Support.",
                business_instructions="Tell customers you are John from support.",
            ),
        )


def test_rollback_control_returns_langgraph_to_custom_without_runtime_repair():
    assert rollback_verified(ROLLBACK_CONTROL) is True
    assert ROLLBACK_CONTROL["from"] == "langgraph_v1"
    assert ROLLBACK_CONTROL["to"] == "custom_v1"
    assert ROLLBACK_CONTROL["requires_database_repair"] is False
    assert ROLLBACK_CONTROL["requires_queue_reset"] is False
    assert ROLLBACK_CONTROL["requires_worker_restart"] is False


def test_observability_signals_cover_activation_acceptance_requirements():
    assert {
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
    } <= OBSERVABILITY_SIGNALS


def test_observability_evidence_maps_all_required_signals():
    assert OBSERVABILITY_SIGNALS <= set(OBSERVABILITY_EVIDENCE)


def test_controlled_activation_plan_is_valid_but_not_executable_now():
    assert activation_plan_valid(CONTROLLED_ACTIVATION_PLAN) is True
    assert CONTROLLED_ACTIVATION_PLAN["execute_now"] is False
    assert "duplicate AI replies" in CONTROLLED_ACTIVATION_PLAN["stop_conditions"]
    assert CONTROLLED_ACTIVATION_PLAN["on_stop_condition"][-1] == "HOLD"


def test_pre_activation_gate_stays_closed_without_external_ci_and_runtime():
    report = pre_activation_gate_report()
    checks = {check.key: check for check in report.checks}

    assert report.ready is False
    assert checks["scenario_matrix"].passed is True
    assert checks["production_policy_draft"].passed is True
    assert checks["rollback"].passed is True
    assert checks["observability"].passed is True
    assert checks["ci_green"].passed is False
    assert checks["langgraph_runtime_verified"].passed is False


def test_pre_activation_gate_can_pass_when_external_checks_are_green(monkeypatch):
    monkeypatch.setattr(readiness, "langgraph_runtime_module_present", lambda: True)

    report = pre_activation_gate_report(
        ci_green=True,
        langgraph_runtime_verified=True,
        focused_tests_green=True,
        postgres_tests_green=True,
        integration_tests_green=True,
        queue_regressions_green=True,
    )

    assert report.ready is True


@pytest.mark.parametrize(
    "scenario", NATURAL_CONVERSATION_SCENARIOS, ids=lambda item: item.key
)
def test_natural_conversation_acceptance_scenarios(
    scenario: NaturalConversationScenario,
):
    score = evaluate_natural_conversation(scenario)

    assert score.passed is True
    assert score.naturalness is True
    assert score.context_awareness is True
    assert score.repetition is True
    assert score.robotic_wording is True
    assert score.unnecessary_questions is True
    assert score.ownership_transition is True
    assert score.duplicate_queue_messaging is True
    assert score.issues == ()


def test_natural_conversation_fails_robotic_internal_workflow_wording():
    scenario = NaturalConversationScenario(
        key="robotic",
        title="Robotic workflow language",
        customer_turns=("My internet is not browsing.",),
        ai_responses=(
            "Intent detected. Classification complete. Routing decision pending.",
        ),
        expected_flags=frozenset({"acknowledges_issue"}),
    )

    score = evaluate_natural_conversation(scenario)

    assert score.passed is False
    assert score.robotic_wording is False


def test_natural_conversation_fails_repeated_unnecessary_ai_disclosure():
    scenario = NaturalConversationScenario(
        key="ai_repeat",
        title="Repeated AI disclosure",
        customer_turns=("My internet is not browsing.",),
        ai_responses=(
            "I am an AI virtual assistant. I am an AI virtual assistant. What is your Portal ID?",
        ),
        expected_flags=frozenset({"acknowledges_issue"}),
    )

    score = evaluate_natural_conversation(scenario)

    assert score.passed is False
    assert score.repetition is False


def test_natural_conversation_fails_langgraph_queue_position_generation():
    scenario = NaturalConversationScenario(
        key="queue_number",
        title="AI generated queue number",
        customer_turns=("I want to speak to an agent.",),
        ai_responses=("You are number 2 in the queue.",),
        expected_flags=frozenset({"team_inbox_queue_authoritative"}),
        queued=True,
    )

    score = evaluate_natural_conversation(scenario)

    assert score.passed is False
    assert score.ownership_transition is False
    assert score.issues == ("ownership_transition",)


def test_natural_conversation_fails_ai_response_after_human_ownership():
    scenario = NaturalConversationScenario(
        key="human_owned_reply",
        title="AI responded after human ownership",
        customer_turns=("Hello?",),
        ai_responses=("I can continue troubleshooting this for you.",),
        expected_flags=frozenset({"ai_ownership_ended"}),
        human_owned=True,
    )

    score = evaluate_natural_conversation(scenario)

    assert score.passed is False
    assert score.ownership_transition is False


def test_generic_runner_executes_new_scenario_without_scenario_specific_python():
    scenario = CanaryScenarioDefinition(
        scenario_id="admin_created_greeting",
        name="Admin Created Greeting",
        engine_requirement=CanaryEngineMode.custom_v1,
        inbound_turns=(CanaryInboundTurn(sequence=1, text="Hello"),),
        assertions=(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.response_contains,
                text="Welcome to Dotmac Support",
            ),
            CanaryAssertion(
                assertion_type=CanaryAssertionType.max_outbound_count, max_count=1
            ),
        ),
    )

    result = run_canary_scenario(scenario)

    assert result.passed is True
    assert result.evidence.scenario_id == "admin_created_greeting"
    assert result.evidence.safety["sends_real_messages"] is False


def test_policy_selection_changes_result_without_runner_code_change():
    scenario = CanaryScenarioDefinition(
        scenario_id="policy_driven_intent",
        name="Policy Driven Intent",
        inbound_turns=(CanaryInboundTurn(sequence=1, text="My internet is down."),),
        assertions=(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.intent_equals,
                expected="technical_support",
            ),
        ),
    )

    default_result = run_canary_scenario(scenario)
    changed_policy_result = run_canary_scenario(
        scenario,
        policy_selection=CanaryPolicySelection(
            intent_aliases={"technical_support": "billing_issue"}
        ),
    )

    assert default_result.passed is True
    assert changed_policy_result.passed is False


def test_production_pattern_discovery_generates_approvable_draft_scenario():
    draft = readiness.approve_production_pattern_as_draft(
        ProductionPatternObservation(
            pattern_id="weak_media_text",
            title="Weak Media Text",
            example_text="image",
            tags=("media",),
            media_type=CanaryMediaType.image,
        )
    )

    result = run_canary_scenario(draft.model_copy(update={"enabled": True}))

    assert draft.enabled is False
    assert "production-derived" in draft.tags
    assert result.passed is True


def test_unknown_assertion_type_is_rejected_before_execution():
    with pytest.raises(ValueError):
        CanaryAssertion.model_validate({"assertion_type": "python_eval"})


def test_generic_media_scenario_uses_event_definition_not_scenario_name():
    scenario = CanaryScenarioDefinition(
        scenario_id="new_media_case",
        name="New Media Case",
        inbound_turns=(
            CanaryInboundTurn(
                sequence=1,
                event_type=CanaryEventType.customer_media,
                text="image",
                media_attached=True,
                media_type=CanaryMediaType.image,
            ),
        ),
        assertions=(
            CanaryAssertion(assertion_type=CanaryAssertionType.media_handoff_occurred),
            CanaryAssertion(assertion_type=CanaryAssertionType.media_not_interpreted),
            CanaryAssertion(assertion_type=CanaryAssertionType.private_note_created),
        ),
    )

    result = run_canary_scenario(scenario)

    assert result.passed is True
    assert result.evidence.handoff_decision is True


def test_generic_rapid_burst_keeps_one_burst_group_and_bounded_outbound():
    scenario = CanaryScenarioDefinition(
        scenario_id="new_rapid_burst",
        name="New Rapid Burst",
        engine_requirement=CanaryEngineMode.custom_v1,
        inbound_turns=(
            CanaryInboundTurn(
                sequence=1, text="My internet is down.", burst_group="b1"
            ),
            CanaryInboundTurn(
                sequence=2,
                text="LOS is red.",
                delay_ms=100,
                burst_group="b1",
            ),
            CanaryInboundTurn(
                sequence=3,
                text="I restarted already.",
                delay_ms=150,
                burst_group="b1",
            ),
        ),
        assertions=(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.fact_equals,
                field="customer_fact.LOS",
                value="red",
            ),
            CanaryAssertion(
                assertion_type=CanaryAssertionType.max_outbound_count, max_count=3
            ),
            CanaryAssertion(assertion_type=CanaryAssertionType.no_duplicate_outbound),
        ),
    )

    result = run_canary_scenario(scenario)

    assert result.passed is True
    assert {turn["burst_group"] for turn in result.evidence.message_turns} == {"b1"}


def test_tool_results_use_approved_typed_schema():
    scenario = CanaryScenarioDefinition(
        scenario_id="monitoring_no_data_generic",
        name="Monitoring No Data Generic",
        engine_requirement=CanaryEngineMode.custom_v1,
        inbound_turns=(CanaryInboundTurn(sequence=1, text="My internet is down."),),
        simulated_tool_results=(
            CanaryToolResult(
                tool_name=CanaryToolName.subscriber_monitoring,
                status=CanaryToolStatus.no_data,
                monitoring_status="no_data",
                monitoring_provenance="simulated_read_only",
            ),
        ),
        assertions=(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.tool_called,
                expected="subscriber_monitoring",
            ),
            CanaryAssertion(
                assertion_type=CanaryAssertionType.monitoring_status_equals,
                expected="no_data",
            ),
        ),
    )

    result = run_canary_scenario(scenario)

    assert result.passed is True


def test_historical_run_evidence_is_tied_to_scenario_and_policy_versions():
    scenario = CanaryScenarioDefinition(
        scenario_id="versioned_scenario",
        name="Versioned Scenario",
        revision=7,
        inbound_turns=(CanaryInboundTurn(sequence=1, text="Hello"),),
        assertions=(
            CanaryAssertion(
                assertion_type=CanaryAssertionType.policy_version_equals,
                expected=12,
            ),
        ),
    )

    result = run_canary_scenario(
        scenario,
        policy_selection=CanaryPolicySelection(policy_version_number=12),
    )

    assert result.passed is True
    assert result.evidence.scenario_revision == 7
    assert result.evidence.policy_version_number == 12


def test_required_scenario_failure_blocks_activation_gate(monkeypatch):
    monkeypatch.setattr(
        readiness, "activation_required_canaries_pass", lambda **_: False
    )

    report = readiness.pre_activation_gate_report(
        ci_green=True,
        langgraph_runtime_verified=True,
        focused_tests_green=True,
        postgres_tests_green=True,
        integration_tests_green=True,
        queue_regressions_green=True,
    )
    checks = {check.key: check for check in report.checks}

    assert report.ready is False
    assert checks["required_canaries"].passed is False


def test_simulation_safety_forbids_real_inbox_customer_queue_mutation():
    result = run_canary_scenario(
        CanaryScenarioDefinition(
            scenario_id="safety",
            name="Safety",
            inbound_turns=(CanaryInboundTurn(sequence=1, text="I want an agent."),),
            assertions=(
                CanaryAssertion(assertion_type=CanaryAssertionType.handoff_required),
            ),
        )
    )

    assert result.passed is True
    assert set(result.evidence.safety.values()) == {False}
