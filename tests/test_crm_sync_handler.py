"""Tests for outbound CRM sync (Sub → DotMac Omni CRM).

Covers the async handoff added so a slow/unreachable CRM never blocks the
request thread, plus the task-level retry contract:

- CrmSyncHandler: enqueues a Celery task (never blocks on HTTP inline),
  guards on CRM config, resolves the Splynx id, and degrades silently.
- push_subscriber_change task: returns True on success, raises CrmPushError
  (to drive Celery autoretry) on failure.
- crm_webhook payload builders: shape + null-speed handling.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services.crm_webhook import (
    native_status,
    native_subscriber_payload,
    service_activation_payload,
    status_change_payload,
)
from app.services.events.handlers.crm_sync import CRM_SYNC_EVENTS, CrmSyncHandler
from app.services.events.types import Event, EventType
from app.tasks.crm_sync import CrmPushError, push_subscriber_change


@contextmanager
def crm_base_url(value: str):
    """Temporarily set settings.crm_base_url (frozen dataclass → object.__setattr__)."""
    original = settings.crm_base_url
    object.__setattr__(settings, "crm_base_url", value)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_base_url", original)


def _subscriber(splynx_id=4242, first="Jane", last="Doe"):
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.splynx_customer_id = splynx_id
    sub.first_name = first
    sub.last_name = last
    sub.display_name = None
    sub.email = "jane@example.com"
    sub.subscriber_number = "SUB-1001"
    sub.account_number = None
    sub.status = "active"
    return sub


def _offer(name="Fiber 100", down=100, up=20):
    offer = MagicMock()
    offer.name = name
    offer.speed_download_mbps = down
    offer.speed_upload_mbps = up
    return offer


# ---------------------------------------------------------------------------
# crm_webhook payload builders
# ---------------------------------------------------------------------------


class TestPayloadBuilders:
    def test_status_change_payload(self):
        payload = status_change_payload("blocked", "Jane Doe")
        assert payload["status"] == "blocked"
        assert payload["name"] == "Jane Doe"
        assert "last_update" in payload

    def test_service_activation_payload(self):
        payload = service_activation_payload("Fiber 100", "100/20 Mbps", "active")
        assert payload["status"] == "active"
        assert payload["service_name"] == "Fiber 100"
        assert payload["service_speed"] == "100/20 Mbps"
        assert "last_update" in payload

    def test_native_status_maps_local_vocabulary(self):
        assert native_status("active") == "active"
        assert native_status("blocked") == "suspended"
        assert native_status("canceled") == "terminated"
        assert native_status("new") == "pending"
        assert native_status(None) == "pending"

    def test_native_subscriber_payload_uses_crm_columns_only(self):
        sub = _subscriber(splynx_id=None)
        payload = native_subscriber_payload(sub, "Fiber 100", "100/20 Mbps")
        # CRM generic handler instantiates its model from this verbatim —
        # every key must be a CRM Subscriber column.
        assert set(payload) <= {
            "status",
            "notes",
            "subscriber_number",
            "account_number",
            "service_name",
            "service_plan",
            "service_speed",
        }
        assert payload["status"] == "active"
        assert payload["subscriber_number"] == "SUB-1001"
        assert "Jane Doe" in payload["notes"]
        assert "jane@example.com" in payload["notes"]


# ---------------------------------------------------------------------------
# CrmSyncHandler — async enqueue behaviour
# ---------------------------------------------------------------------------


class TestCrmSyncHandler:
    def _handle(self, event, db):
        """Run the handler with enqueue + HTTP mocked; return (enqueue_mock, http_mock)."""
        with (
            patch("app.services.queue_adapter.enqueue_task") as enqueue,
            patch("app.services.crm_webhook.post") as http,
        ):
            CrmSyncHandler().handle(db, event)
        return enqueue, http

    def test_ignores_unmapped_events(self):
        event = Event(event_type=EventType.dunning_started, payload={})
        with crm_base_url("https://crm.example"):
            enqueue, http = self._handle(event, MagicMock())
        enqueue.assert_not_called()
        http.assert_not_called()

    def test_suspended_enqueues_status_change(self):
        db = MagicMock()
        db.get.return_value = _subscriber()
        event = Event(
            event_type=EventType.subscriber_suspended,
            payload={"to_status": "blocked"},
            account_id=uuid.uuid4(),
        )
        with crm_base_url("https://crm.example"):
            enqueue, http = self._handle(event, db)

        # Enqueued, and NO inline HTTP (the whole point: non-blocking).
        http.assert_not_called()
        enqueue.assert_called_once()
        _, kwargs = enqueue.call_args
        splynx_id, payload, external_system = kwargs["args"]
        assert splynx_id == 4242
        assert external_system == "splynx"
        assert payload["status"] == "blocked"
        assert payload["name"] == "Jane Doe"
        assert kwargs["source"] == "crm_sync_handler"
        assert kwargs["correlation_id"] == f"crm_sync:{event.event_id}"

    def test_reactivated_defaults_to_active(self):
        db = MagicMock()
        db.get.return_value = _subscriber()
        event = Event(
            event_type=EventType.subscriber_reactivated,
            payload={},
            account_id=uuid.uuid4(),
        )
        with crm_base_url("https://crm.example"):
            enqueue, _ = self._handle(event, db)
        _, kwargs = enqueue.call_args
        _, payload, _ = kwargs["args"]
        assert payload["status"] == "active"

    def test_subscription_activated_enqueues_service(self):
        sub = _subscriber()
        subscription = MagicMock()
        subscription.offer = _offer()
        db = MagicMock()
        db.get.side_effect = lambda model, _id: (
            sub if model.__name__ == "Subscriber" else subscription
        )
        event = Event(
            event_type=EventType.subscription_activated,
            payload={},
            account_id=uuid.uuid4(),
            subscription_id=uuid.uuid4(),
        )
        with crm_base_url("https://crm.example"):
            enqueue, http = self._handle(event, db)

        http.assert_not_called()
        enqueue.assert_called_once()
        _, kwargs = enqueue.call_args
        splynx_id, payload, external_system = kwargs["args"]
        assert splynx_id == 4242
        assert external_system == "splynx"
        assert payload["status"] == "active"
        assert payload["service_name"] == "Fiber 100"
        assert payload["service_speed"] == "100/20 Mbps"

    def test_service_speed_blank_when_upload_null(self):
        """Regression: null speed_upload_mbps must not render '100/None Mbps'."""
        sub = _subscriber()
        subscription = MagicMock()
        subscription.offer = _offer(up=None)
        db = MagicMock()
        db.get.side_effect = lambda model, _id: (
            sub if model.__name__ == "Subscriber" else subscription
        )
        event = Event(
            event_type=EventType.subscription_activated,
            payload={},
            account_id=uuid.uuid4(),
            subscription_id=uuid.uuid4(),
        )
        with crm_base_url("https://crm.example"):
            enqueue, _ = self._handle(event, db)
        _, kwargs = enqueue.call_args
        _, payload, _ = kwargs["args"]
        assert payload["service_name"] == "Fiber 100"
        assert payload["service_speed"] == ""

    def test_skips_when_crm_unconfigured(self):
        db = MagicMock()
        db.get.return_value = _subscriber()
        event = Event(
            event_type=EventType.subscriber_suspended,
            payload={"to_status": "blocked"},
            account_id=uuid.uuid4(),
        )
        with crm_base_url(""):
            enqueue, http = self._handle(event, db)
        enqueue.assert_not_called()
        http.assert_not_called()

    def test_native_subscriber_pushes_generic_webhook(self):
        """Subscribers without a Splynx id push via the generic 'dotmac' system."""
        sub = _subscriber(splynx_id=None)
        db = MagicMock()
        db.get.return_value = sub
        event = Event(
            event_type=EventType.subscriber_suspended,
            payload={"to_status": "blocked"},
            account_id=uuid.uuid4(),
        )
        with crm_base_url("https://crm.example"):
            enqueue, _ = self._handle(event, db)
        enqueue.assert_called_once()
        _, kwargs = enqueue.call_args
        external_id, payload, external_system = kwargs["args"]
        assert external_id == str(sub.id)
        assert external_system == "dotmac"
        assert payload["status"] == "suspended"  # CRM vocabulary, not splynx's
        assert "name" not in payload  # not a CRM Subscriber column

    def test_subscriber_created_pushes_native_only(self):
        """Creation pushes natives into the CRM; migrated subscribers skip."""
        native = _subscriber(splynx_id=None)
        db = MagicMock()
        db.get.return_value = native
        event = Event(
            event_type=EventType.subscriber_created,
            payload={},
            account_id=uuid.uuid4(),
        )
        with crm_base_url("https://crm.example"):
            enqueue, _ = self._handle(event, db)
        enqueue.assert_called_once()
        _, kwargs = enqueue.call_args
        external_id, payload, external_system = kwargs["args"]
        assert external_id == str(native.id)
        assert external_system == "dotmac"
        assert payload["status"] == "active"

        db.get.return_value = _subscriber()  # has a splynx id → already in CRM
        with crm_base_url("https://crm.example"):
            enqueue, _ = self._handle(event, db)
        enqueue.assert_not_called()

    def test_skips_when_no_account_id(self):
        event = Event(
            event_type=EventType.subscriber_suspended,
            payload={"to_status": "blocked"},
        )
        with crm_base_url("https://crm.example"):
            enqueue, _ = self._handle(event, MagicMock())
        enqueue.assert_not_called()

    def test_enqueue_failure_is_swallowed(self):
        """A queue error must not bubble up and break the emitting transaction."""
        db = MagicMock()
        db.get.return_value = _subscriber()
        event = Event(
            event_type=EventType.subscriber_suspended,
            payload={"to_status": "blocked"},
            account_id=uuid.uuid4(),
        )
        with (
            crm_base_url("https://crm.example"),
            patch(
                "app.services.queue_adapter.enqueue_task",
                side_effect=RuntimeError("broker down"),
            ),
        ):
            # handle() wraps _dispatch in try/except — must not raise.
            CrmSyncHandler().handle(db, event)

    def test_all_sync_events_covered(self):
        # Guard against silently dropping an event type from the dispatch map.
        assert EventType.subscriber_suspended in CRM_SYNC_EVENTS
        assert EventType.subscription_activated in CRM_SYNC_EVENTS
        assert EventType.subscription_canceled in CRM_SYNC_EVENTS


# ---------------------------------------------------------------------------
# push_subscriber_change task — retry contract
# ---------------------------------------------------------------------------


class TestPushTask:
    def test_returns_true_on_success(self):
        with patch(
            "app.services.crm_webhook.push_subscriber_change", return_value="ok"
        ) as push:
            assert push_subscriber_change.run(99, {"status": "active"}) is True
        push.assert_called_once_with(99, {"status": "active"}, "splynx")

    def test_raises_to_retry_on_failure(self):
        with patch(
            "app.services.crm_webhook.push_subscriber_change", return_value=None
        ):
            with pytest.raises(CrmPushError):
                push_subscriber_change.run(99, {"status": "active"})

    def test_native_push_persists_crm_link(self):
        local_id = str(uuid.uuid4())
        crm_id = str(uuid.uuid4())
        with (
            patch(
                "app.services.crm_webhook.push_subscriber_change",
                return_value=crm_id,
            ),
            patch("app.tasks.crm_sync._persist_crm_link") as persist,
        ):
            assert (
                push_subscriber_change.run(local_id, {"status": "active"}, "dotmac")
                is True
            )
        persist.assert_called_once_with(local_id, crm_id)

    def test_splynx_push_does_not_persist(self):
        with (
            patch(
                "app.services.crm_webhook.push_subscriber_change",
                return_value=str(uuid.uuid4()),
            ),
            patch("app.tasks.crm_sync._persist_crm_link") as persist,
        ):
            push_subscriber_change.run(99, {"status": "active"})
        persist.assert_not_called()

    def test_task_configured_for_autoretry(self):
        assert CrmPushError in push_subscriber_change.autoretry_for
        assert push_subscriber_change.max_retries == 8
