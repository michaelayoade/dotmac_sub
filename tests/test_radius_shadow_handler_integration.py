"""Access-state ownership and durable enforcement projection tests."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.events.handlers.enforcement import (
    EnforcementHandler,
    EnforcementProjectionError,
)
from app.services.events.types import Event, EventType

ROOT = Path(__file__).resolve().parents[1]


def _stub_subscription(*, status: SubscriptionStatus) -> MagicMock:
    subscription = MagicMock(spec=Subscription)
    subscription.id = uuid4()
    subscription.subscriber_id = uuid4()
    subscription.status = status
    return subscription


def test_account_lifecycle_is_the_only_access_state_writer() -> None:
    writers: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute) and target.attr == "access_state"
                for target in targets
            ):
                writers.add(path.relative_to(ROOT).as_posix())

    assert writers == {"app/services/account_lifecycle.py"}


def test_enforcement_adapter_has_no_parallel_access_state_path() -> None:
    source = (ROOT / "app/services/events/handlers/enforcement.py").read_text(
        encoding="utf-8"
    )

    assert "_shadow_write_access_state" not in source
    assert "set_subscription_access_state" not in source


def test_block_projection_failure_suppresses_cleanup_and_remains_retryable() -> None:
    handler = EnforcementHandler()
    db = MagicMock()
    subscription = _stub_subscription(status=SubscriptionStatus.suspended)
    db.get.return_value = subscription
    with (
        patch("app.services.events.handlers.enforcement.radius_reject_service"),
        patch(
            "app.services.events.handlers.enforcement.radius_service."
            "reconcile_subscription_connectivity",
            side_effect=RuntimeError("projection incomplete"),
        ),
        patch.object(handler, "_enqueue_subscription_session_cleanup") as cleanup,
        pytest.raises(EnforcementProjectionError, match="projection incomplete"),
    ):
        handler._enforce_subscription_block(db, str(subscription.id))

    cleanup.assert_not_called()


def test_restore_projection_failure_suppresses_session_changes() -> None:
    handler = EnforcementHandler()
    db = MagicMock()
    subscription = _stub_subscription(status=SubscriptionStatus.active)
    db.get.return_value = subscription
    event = Event(
        event_type=EventType.subscription_resumed,
        payload={"subscription_id": str(subscription.id)},
        subscription_id=subscription.id,
    )
    with (
        patch("app.services.account_lifecycle.compute_account_status"),
        patch("app.services.events.handlers.enforcement.radius_reject_service"),
        patch(
            "app.services.events.handlers.enforcement.radius_service."
            "reconcile_subscription_connectivity",
            side_effect=RuntimeError("projection incomplete"),
        ),
        patch(
            "app.services.events.handlers.enforcement.disconnect_subscription_sessions"
        ) as disconnect,
        patch(
            "app.services.events.handlers.enforcement."
            "remove_subscription_address_list_block"
        ) as remove_block,
        pytest.raises(EnforcementProjectionError, match="projection incomplete"),
    ):
        handler._handle_subscription_restore(db, event)

    disconnect.assert_not_called()
    remove_block.assert_not_called()
