from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services.control_relationships import (
    ControlRelationshipError,
    audit_setting_relationships,
    event_topology,
    relationship_manifest,
    validate_and_order_handlers,
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
    findings = audit_setting_relationships(
        db_session,
        pending=(SettingDomain.projects, "quotes_native_write_enabled", True),
    )

    assert {item.code for item in findings} == {"quote_write_before_read_flip"}


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
