from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services.control_relationships import (
    ControlRelationshipError,
    audit_event_relationships,
    audit_feature_control_relationships,
    event_execution_plan,
    event_policies,
    event_topology,
    handler_event_types,
    relationship_manifest,
    validate_and_order_handlers,
    validate_event_execution_policy,
    validate_feature_control_changes,
    validate_setting_change,
)
from app.services.domain_settings import billing_settings
from app.services.events.dispatcher import EventDispatcher
from app.services.events.types import Event, EventType


def _handler(name: str):
    return type(name, (), {})()


def test_handler_topology_orders_state_before_communications_and_external():
    handlers = [
        _handler("WebhookHandler"),
        _handler("NotificationHandler"),
        _handler("EnforcementHandler"),
        _handler("LifecycleHandler"),
    ]

    ordered = validate_and_order_handlers(handlers)

    assert [item.__class__.__name__ for item in ordered] == [
        "LifecycleHandler",
        "EnforcementHandler",
        "NotificationHandler",
        "WebhookHandler",
    ]


def test_handler_topology_rejects_duplicates_and_undeclared_handlers():
    with pytest.raises(ControlRelationshipError, match="Duplicate"):
        validate_and_order_handlers(
            [_handler("WebhookHandler"), _handler("WebhookHandler")]
        )
    with pytest.raises(ControlRelationshipError, match="missing control"):
        validate_and_order_handlers([_handler("UnknownHandler")])


def test_payment_failover_providers_are_mutually_exclusive(db_session):
    with pytest.raises(ControlRelationshipError, match="must differ"):
        validate_setting_change(
            db_session,
            SettingDomain.billing,
            "payment_gateway_secondary_provider",
            "paystack",
        )


def test_domain_setting_mutation_enforces_relationship_registry(db_session):
    with pytest.raises(HTTPException) as exc_info:
        billing_settings.upsert_by_key(
            db_session,
            "payment_gateway_secondary_provider",
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text="paystack",
                is_active=True,
            ),
        )

    assert exc_info.value.status_code == 400
    assert "must differ" in str(exc_info.value.detail)


def test_quote_migration_chain_flags_unsafe_order(db_session):
    findings = audit_feature_control_relationships(
        db_session,
        pending={"quotes.native_write": True},
    )

    assert {item.code for item in findings} == {"quote_write_before_read_flip"}


def test_canonical_feature_writer_enforces_quote_migration_chain(db_session):
    with pytest.raises(ControlRelationshipError, match="require the native read"):
        validate_feature_control_changes(
            db_session, {"quotes.native_write": True, "quotes.native_read": False}
        )


def test_control_manifest_covers_all_relationship_modes():
    modes = {item["mode"] for item in relationship_manifest()}
    assert modes == {
        "exclusive",
        "precedence",
        "chain",
        "fanout",
        "competing",
        "incompatible",
    }
    assert event_topology()[0]["stage"] == "state"


def test_chained_event_failure_blocks_later_handlers():
    first = MagicMock()
    first.__class__.__name__ = "FirstHandler"
    first.handle.side_effect = RuntimeError("state failed")
    second = MagicMock()
    second.__class__.__name__ = "SecondHandler"
    dispatcher = EventDispatcher()
    dispatcher.register_handler(first)
    dispatcher.register_handler(second)

    dispatcher.dispatch(
        MagicMock(),
        Event(event_type=EventType.subscription_activated, payload={}),
    )

    first.handle.assert_called_once()
    second.handle.assert_not_called()


def test_fanout_event_failure_does_not_block_later_handlers():
    first = MagicMock()
    first.__class__.__name__ = "FirstHandler"
    first.handle.side_effect = RuntimeError("fanout failed")
    second = MagicMock()
    dispatcher = EventDispatcher()
    dispatcher.register_handler(first)
    dispatcher.register_handler(second)

    dispatcher.dispatch(
        MagicMock(), Event(event_type=EventType.subscriber_created, payload={})
    )

    second.handle.assert_called_once()


def test_payment_and_overdue_events_are_independent_fanout_consequences():
    handlers = [
        _handler("ArrangementHandler"),
        _handler("EnforcementHandler"),
        _handler("NotificationHandler"),
        _handler("WebhookHandler"),
        _handler("IntegrationHookHandler"),
    ]

    for event_type in (EventType.payment_received, EventType.invoice_overdue):
        plan = event_execution_plan(event_type.value, handlers)
        assert plan
        assert all(step.dependencies == () for step in plan)


def test_activation_plan_keeps_state_peers_independent_and_stages_outputs():
    handlers = validate_and_order_handlers(
        [
            _handler("IntegrationHookHandler"),
            _handler("WebhookHandler"),
            _handler("NotificationHandler"),
            _handler("EnforcementHandler"),
            _handler("ProvisioningHandler"),
            _handler("ReferralHandler"),
            _handler("LifecycleHandler"),
        ]
    )

    plan = event_execution_plan(EventType.subscription_activated.value, handlers)
    by_name = {step.handler_name: step for step in plan}

    assert by_name["LifecycleHandler"].dependencies == ()
    assert by_name["ReferralHandler"].dependencies == ()
    assert by_name["ProvisioningHandler"].dependencies == ()
    assert by_name["EnforcementHandler"].dependencies == ("ProvisioningHandler",)
    assert set(by_name["NotificationHandler"].dependencies) == {
        "LifecycleHandler",
        "ReferralHandler",
        "ProvisioningHandler",
        "EnforcementHandler",
    }
    assert "NotificationHandler" in by_name["WebhookHandler"].dependencies
    assert "NotificationHandler" in by_name["IntegrationHookHandler"].dependencies


def test_chained_state_failure_does_not_block_independent_state_peer():
    lifecycle = _handler("LifecycleHandler")
    lifecycle.handle = MagicMock(side_effect=RuntimeError("audit failed"))
    referral = _handler("ReferralHandler")
    referral.handle = MagicMock()
    notification = _handler("NotificationHandler")
    notification.handle = MagicMock()
    dispatcher = EventDispatcher()
    dispatcher.register_handler(lifecycle)
    dispatcher.register_handler(referral)
    dispatcher.register_handler(notification)

    dispatcher.dispatch(
        MagicMock(),
        Event(event_type=EventType.subscription_activated, payload={}),
    )

    referral.handle.assert_called_once()
    notification.handle.assert_not_called()


def test_dispatcher_skips_handlers_outside_their_event_scope():
    arrangement = _handler("ArrangementHandler")
    arrangement.handle = MagicMock()
    integration = _handler("IntegrationHookHandler")
    integration.handle = MagicMock()
    dispatcher = EventDispatcher()
    dispatcher.register_handler(arrangement)
    dispatcher.register_handler(integration)

    dispatcher.dispatch(
        MagicMock(), Event(event_type=EventType.subscriber_created, payload={})
    )

    arrangement.handle.assert_not_called()
    integration.handle.assert_called_once()


def test_retry_runs_failed_predecessor_before_blocked_dependent():
    order: list[str] = []
    lifecycle = _handler("LifecycleHandler")
    lifecycle.handle = MagicMock()
    provisioning = _handler("ProvisioningHandler")
    provisioning.handle = MagicMock(
        side_effect=lambda *_args: order.append("provisioning")
    )
    enforcement = _handler("EnforcementHandler")
    enforcement.handle = MagicMock(
        side_effect=lambda *_args: order.append("enforcement")
    )
    dispatcher = EventDispatcher()
    dispatcher.register_handler(lifecycle)
    dispatcher.register_handler(provisioning)
    dispatcher.register_handler(enforcement)

    event_record = MagicMock()
    event_record.id = uuid.uuid4()
    event_record.event_id = uuid.uuid4()
    event_record.event_type = EventType.subscription_activated.value
    event_record.payload = {}
    event_record.actor = None
    event_record.subscriber_id = None
    event_record.account_id = None
    event_record.subscription_id = None
    event_record.invoice_id = None
    event_record.service_order_id = None
    event_record.failed_handlers = [
        {"handler": "ProvisioningHandler", "error": "failed"},
        {
            "handler": "EnforcementHandler",
            "error": "blocked",
            "blocked_by": "ProvisioningHandler",
        },
    ]
    event_record.retry_count = 0

    assert dispatcher.retry_event(MagicMock(), event_record) is True
    lifecycle.handle.assert_not_called()
    provisioning.handle.assert_called_once()
    enforcement.handle.assert_called_once()
    assert order == ["provisioning", "enforcement"]


def test_event_policy_manifest_and_scopes_are_executable():
    handlers = [type(name, (), {})() for name in event_topology_handler_names()]
    validate_event_execution_policy(handlers)

    assert handler_event_types("IntegrationHookHandler") is None
    assert EventType.payment_received.value in (
        handler_event_types("ArrangementHandler") or frozenset()
    )
    assert event_policies()["overrides"]["subscription.activated"]["steps"]
    assert audit_event_relationships() == []


def event_topology_handler_names() -> list[str]:
    return [str(item["handler"]) for item in event_topology()]
