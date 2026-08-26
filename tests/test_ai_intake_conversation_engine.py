from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.ai_intake import AiIntakePolicy, AiIntakePolicyVersion, AiIntakeSession
from app.models.subscriber import (
    Gender,
    Reseller,
    Subscriber,
    SubscriberCategory,
    UserType,
)
from app.models.team_inbox import InboxChannelType, InboxConversation
from app.schemas.ai_intake import (
    AiIntakeCategory,
    AiIntakeClassification,
    AiIntakeIntent,
)
from app.services import (
    ai_conversation_intake,
    ai_intake_graph,
)
from app.services import (
    ai_intake_conversation_engine as engine,
)
from app.services.owner_commands import CommandContext


def _subscriber(db_session, *, email: str | None = None, phone: str | None = None):
    reseller = db_session.query(Reseller).filter(Reseller.is_house.is_(True)).first()
    if reseller is None:
        reseller = Reseller(name=f"Engine House {uuid4()}", is_house=True)
        db_session.add(reseller)
        db_session.flush()
    row = Subscriber(
        email=email or f"engine-{uuid4()}@example.test",
        phone=phone,
        first_name="Engine",
        last_name="Customer",
        user_type=UserType.customer,
        reseller_id=reseller.id,
        gender=Gender.unknown,
    )
    row.category = SubscriberCategory.residential
    row.account_number = f"DM-{uuid4().hex[:6]}"
    db_session.add(row)
    db_session.flush()
    return row


def _conversation(db_session, *, subscriber_id=None):
    row = InboxConversation(
        subscriber_id=subscriber_id,
        channel_type=InboxChannelType.whatsapp.value,
        status="pending",
        contact_address="2348012345678",
        external_thread_id=f"thread-{uuid4()}",
        metadata_={},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _version(db_session, *, metadata=None):
    policy = AiIntakePolicy(
        scope_key=f"meta_cloud_api:{uuid4().hex}",
        channel_type=InboxChannelType.whatsapp.value,
        provider="meta_cloud_api",
        account_scope=f"phone-{uuid4().hex}",
        display_name="Dotmac Virtual Assistant",
        is_enabled=True,
    )
    db_session.add(policy)
    db_session.flush()
    version = AiIntakePolicyVersion(
        policy_id=policy.id,
        version_number=1,
        status="activated",
        is_active=True,
        display_name="Dotmac Virtual Assistant",
        welcome_message="Hello",
        metadata_={
            "conversational_engine_enabled": True,
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": True},
            },
            "permitted_identifiers": [
                "registered_phone",
                "registered_email",
                "portal_id",
            ],
            "conversation_policy": {
                "max_turns": 6,
                "require_identity_before_tools": True,
            },
            **dict(metadata or {}),
        },
    )
    db_session.add(version)
    db_session.flush()
    policy.active_version_id = version.id
    return version


def _session(db_session, conversation, version, *, metadata=None, expires_at=None):
    row = AiIntakeSession(
        conversation_id=conversation.id,
        policy_id=version.policy_id,
        policy_version_id=version.id,
        state="collecting_intent",
        channel_type=conversation.channel_type,
        provider="meta_cloud_api",
        account_scope="phone-1",
        display_name="Dotmac Virtual Assistant",
        max_turns=6,
        confidence_threshold=0.75,
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        metadata_=dict(metadata or {}),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _classification(
    intent="technical_support",
    category="no_internet",
    confidence=0.95,
    *,
    requires_follow_up: bool = False,
    follow_up_question: str | None = None,
):
    return AiIntakeClassification(
        intent=AiIntakeIntent(intent),
        category=AiIntakeCategory(category),
        confidence=confidence,
        department=None,
        requires_follow_up=requires_follow_up,
        follow_up_question=follow_up_question,
        summary="Customer needs support.",
    )


def test_identified_subscriber_does_not_request_portal_id(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet has not been browsing since morning.",
        classification=_classification(),
    )

    assert decision.action == "respond"
    assert "Portal ID" not in (decision.response_text or "")
    assert decision.state.subscriber_id == str(subscriber.id)
    assert decision.state.monitoring_results[-1]["service_state"] == "offline"


def test_portal_id_requested_only_when_needed_and_not_repeated(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"permitted_identifiers": ["portal_id"]},
    )
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is down.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Still down.",
        classification=_classification(),
    )

    assert first.action == "respond"
    assert "Portal ID" in (first.response_text or "")
    assert "portal_id" in first.state.already_requested_fields
    assert second.action == "handoff"


def test_unknown_customer_can_supply_portal_id_on_second_turn(db_session):
    subscriber = _subscriber(db_session)
    subscriber.account_number = "12345"
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"permitted_identifiers": ["portal_id"]},
    )
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is not browsing.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My Portal ID is 12345.",
        classification=_classification(),
    )

    assert first.action == "respond"
    assert "Portal ID" in (first.response_text or "")
    assert second.state.portal_id == "12345"
    assert second.state.subscriber_id == str(subscriber.id)
    assert "portal_id" in second.state.already_requested_fields
    assert second.response_text is None or "Portal ID" not in second.response_text


def test_registered_email_lookup_identifies_customer(db_session):
    subscriber = _subscriber(db_session, email="lookup@example.test")
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My registered email is lookup@example.test and internet is down.",
        classification=_classification(),
    )

    assert decision.state.subscriber_id == str(subscriber.id)
    assert conversation.subscriber_id == subscriber.id
    assert any(
        item["tool"] == "customer_lookup" and item["status"] == "found"
        for item in decision.state.tool_executions
    )


def test_rich_first_message_extracts_existing_facts(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body=(
            "My Portal ID is DM-12345. My internet is down since yesterday "
            "and I have restarted the router twice."
        ),
        classification=_classification(),
    )

    assert decision.state.portal_id == "DM-12345"
    assert decision.state.collected_facts["connectivity_problem"] is True
    assert decision.state.collected_facts["outage_context"] == "since yesterday"
    assert decision.state.collected_facts["router_restarted"] is True
    assert "portal_id" not in decision.state.already_requested_fields
    assert "router_restarted" not in decision.state.already_requested_fields


def test_monitoring_troubleshooting_then_red_los_handoff_retains_state(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Internet is down and I restarted the router twice.",
        classification=_classification(),
        tool_mode="simulation",
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Router is powered and LOS is red.",
        classification=_classification(),
        tool_mode="simulation",
    )

    assert first.action == "respond"
    assert "red warning light" in (first.response_text or "")
    assert second.action == "handoff"
    assert second.state.collected_facts["router_restarted"] is True
    assert second.state.collected_facts["router_powered"] is True
    assert second.state.collected_facts["los_red"] is True
    assert second.state.escalation_reason == "red_los"


def test_monitoring_unavailable_escalates_without_diagnosis(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("monitoring down")

    monkeypatch.setattr(engine, "get_customer_network_context", _raise)
    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="No internet.",
        classification=_classification(),
    )

    assert decision.action == "handoff"
    assert decision.state.escalation_reason == "monitoring_unavailable"
    assert "could not complete" in (decision.response_text or "")


def test_ambiguous_lookup_records_tool_result(db_session):
    _subscriber(db_session, email="shared@example.test")
    _subscriber(db_session, email="shared@example.test")
    conversation = _conversation(db_session)
    version = _version(
        db_session, metadata={"permitted_identifiers": ["registered_email"]}
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My email is shared@example.test and internet is down.",
        classification=_classification(),
    )

    assert any(
        item["tool"] == "customer_lookup" and item["status"] == "ambiguous"
        for item in decision.state.tool_executions
    )


def test_red_los_escalates(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="The router is on. LOS is red.",
        classification=_classification(),
    )

    assert decision.action == "handoff"
    assert decision.state.escalation_reason == "red_los"
    assert "LOS" in (decision.handoff_summary or "")


def test_configuration_driven_troubleshooting_rule(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "troubleshooting_rules": [
                    {
                        "condition": {"fact": "router_restarted", "equals": True},
                        "action": "handoff",
                        "reason": "restart_completed",
                        "response": "I will pass this to support now.",
                    }
                ]
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I restarted the router twice.",
        classification=_classification(category="router_issue"),
    )

    assert decision.action == "handoff"
    assert decision.state.escalation_reason == "restart_completed"


def test_multiple_turns_and_intent_change_are_persisted(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is not working.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Actually it works but has been very slow for three days.",
        classification=_classification(category="slow_internet"),
    )

    assert second.state.current_intent == "technical_support"
    assert second.state.category == "slow_internet"
    assert second.state.collected_facts["slow_internet"] is True
    assert second.state.collected_facts["connectivity_problem"] is False
    assert second.state.turn_count == 2


def test_explicit_human_request_escalates(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I want to speak with a human agent.",
        classification=_classification(),
    )

    assert decision.action == "handoff"
    assert decision.state.human_requested is True
    assert decision.state.escalation_reason == "human_requested"


def test_follow_up_classification_is_not_handed_off_by_engine(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "max_turns": 6,
                "handoff_after_classification": True,
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Hello",
        classification=_classification(
            intent="general_enquiry",
            category="general_enquiry",
            confidence=0.2,
            requires_follow_up=True,
            follow_up_question="Please tell me what you need help with today.",
        ),
    )

    assert decision.action == "continue_classifier"
    assert decision.state.classification_requires_follow_up is True
    assert decision.state.classification_follow_up_question == (
        "Please tell me what you need help with today."
    )


def test_turn_limit_and_timeout_escalate(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session, metadata={"conversation_policy": {"max_turns": 1}})
    session = _session(db_session, conversation, version)
    state = engine.ConversationalState.load(conversation=conversation, session=session)
    state.turn_count = 1
    engine.persist_state(session, state)

    limited = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Still not working.",
        classification=_classification(),
    )

    expired_conversation = _conversation(db_session)
    expired_session = _session(
        db_session,
        expired_conversation,
        version,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    timed_out = engine.run_conversational_turn(
        db_session,
        conversation=expired_conversation,
        session=expired_session,
        version=version,
        latest_body="Still not working.",
        classification=_classification(),
    )

    assert limited.state.escalation_reason == "turn_limit"
    assert timed_out.state.escalation_reason == "timeout"


def test_disabled_and_unauthorized_tools_do_not_execute(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": False},
                "subscriber_monitoring": {"enabled": False},
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My email is noone@example.test and internet is down.",
        classification=_classification(),
    )
    direct = engine.execute_tool(
        db_session,
        "subscriber_monitoring",
        {"subscriber_id": str(uuid4())},
        policy={"tools": {"subscriber_monitoring": {"enabled": False}}},
    )

    assert any(
        item["tool"] == "customer_lookup" and item["status"] == "unauthorized"
        for item in decision.state.tool_executions
    )
    assert direct["status"] == "unauthorized"


def test_simulation_preview_does_not_call_live_lookup(db_session, monkeypatch):
    conversation = _conversation(db_session)
    version = _version(db_session)
    called = False

    def _live_lookup(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("simulation preview must not call live lookup")

    monkeypatch.setattr(engine, "_customer_lookup", _live_lookup)

    result = ai_conversation_intake.preview_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyPreviewCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-preview",
                reason="test simulation preview",
            ),
            version_id=version.id,
            customer_message="My Portal ID is 12345 and internet is down.",
            channel_type=conversation.channel_type,
            preview_mode="simulation",
        ),
    )

    assert called is False
    assert result.preview_mode == "simulation"
    assert any(
        item["tool"] == "customer_lookup" and item["result"]["simulated"] is True
        for item in result.tool_executions
    )


def test_ai_engine_does_not_implement_queue_or_round_robin():
    source = inspect.getsource(engine)

    assert "InboxConversationQueueEntry" not in source
    assert "InboxTeamRoundRobinCursor" not in source
    assert "queue_position" not in source


def test_langgraph_topology_contains_expected_nodes_and_edges():
    topology = ai_intake_graph.graph_topology()

    assert set(ai_intake_graph.GRAPH_NODE_SEQUENCE) <= set(topology)
    assert topology["load_policy"] == ("load_state",)
    assert "request_identifier" in topology["determine_missing_information"]
    assert "execute_tool" in topology["select_tool"]
    assert "handoff" in topology["decide_next_action"]
    assert "resolved" in topology["decide_next_action"]


def test_langgraph_state_hydration_uses_dotmac_session_state(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(
        db_session,
        conversation,
        version,
        metadata={
            engine.STATE_KEY: {
                "conversation_id": str(conversation.id),
                "session_id": "existing-session",
                "policy_version_id": str(version.id),
                "channel": conversation.channel_type,
                "current_intent": "technical_support",
                "collected_facts": {"router_restarted": True},
                "turn_count": 2,
            }
        },
    )

    loaded = engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )

    assert loaded.current_intent == "technical_support"
    assert loaded.collected_facts["router_restarted"] is True
    assert loaded.policy_version_id == str(version.id)


def test_langgraph_module_does_not_own_queue_or_round_robin():
    source = inspect.getsource(ai_intake_graph)

    assert "InboxConversationQueueEntry" not in source
    assert "InboxTeamRoundRobinCursor" not in source
    assert "assign_conversation_to_available_agent" not in source


def test_langgraph_runtime_fallback_is_observable():
    source = inspect.getsource(ai_conversation_intake)

    assert "ai_intake_langgraph_unavailable_falling_back" in source
    assert '"ai_intake_engine_requested"' in source
    assert '"requested_engine"' in source
