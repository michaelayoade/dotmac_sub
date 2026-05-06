"""Tests for TR-069 bootstrap auto-restore functionality.

When an ONT reboots and sends a BOOTSTRAP inform, the system should
automatically re-apply volatile TR-069 config (WiFi, LAN) from desired_config.
"""

import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.tr069 import (
    _queue_service_apply_on_bootstrap,
    _ont_has_saved_service_intent,
)


class TestQueueServiceApplyOnBootstrap:
    """Tests for _queue_service_apply_on_bootstrap function."""

    def test_returns_false_if_ont_id_is_none(self, db_session):
        """Should return False when ont_id is None."""
        result = _queue_service_apply_on_bootstrap(
            db_session,
            ont_id=None,
            event_type="bootstrap",
        )
        assert result is False

    def test_returns_false_for_non_bootstrap_events(self, db_session):
        """Should return False for events other than bootstrap/boot."""
        result = _queue_service_apply_on_bootstrap(
            db_session,
            ont_id=str(uuid4()),
            event_type="periodic",
        )
        assert result is False

        result = _queue_service_apply_on_bootstrap(
            db_session,
            ont_id=str(uuid4()),
            event_type="value_change",
        )
        assert result is False

    def test_returns_false_for_ont_without_service_intent(self, db_session):
        """Should return False when ONT has no saved WiFi/WAN config."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={},  # No WiFi config
        )
        db_session.add(ont)
        db_session.flush()

        result = _queue_service_apply_on_bootstrap(
            db_session,
            ont_id=str(ont.id),
            event_type="bootstrap",
        )
        assert result is False

    @patch("app.services.queue_adapter.enqueue_task")
    def test_queues_task_on_bootstrap_with_wifi_config(self, mock_enqueue, db_session):
        """Should queue service apply task when ONT has WiFi config."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={
                "wifi": {
                    "ssid": "TestNetwork",
                    "password": "TestPass123",
                }
            },
        )
        db_session.add(ont)
        db_session.flush()

        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_enqueue.return_value = mock_dispatch

        result = _queue_service_apply_on_bootstrap(
            db_session,
            ont_id=str(ont.id),
            event_type="bootstrap",
        )

        assert result is True
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args
        assert call_args[0][0] == "app.tasks.tr069.apply_saved_ont_service_config"
        assert str(ont.id) in call_args[1]["args"]
        assert "bootstrap_reconnect" in call_args[1]["args"]

    @patch("app.services.queue_adapter.enqueue_task")
    def test_queues_task_on_boot_event(self, mock_enqueue, db_session):
        """Should also queue on 'boot' event (variant of bootstrap)."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={
                "wifi": {
                    "ssid": "TestNetwork",
                }
            },
        )
        db_session.add(ont)
        db_session.flush()

        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_enqueue.return_value = mock_dispatch

        result = _queue_service_apply_on_bootstrap(
            db_session,
            ont_id=str(ont.id),
            event_type="boot",
        )

        assert result is True
        call_args = mock_enqueue.call_args
        assert "boot_reconnect" in call_args[1]["args"]


class TestOntHasSavedServiceIntent:
    """Tests for _ont_has_saved_service_intent function."""

    def test_returns_false_for_nonexistent_ont(self, db_session):
        """Should return False when ONT doesn't exist."""
        result = _ont_has_saved_service_intent(db_session, str(uuid4()))
        assert result is False

    def test_returns_false_for_inactive_ont(self, db_session):
        """Should return False when ONT is inactive."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=False,
            desired_config={"wifi": {"ssid": "Test"}},
        )
        db_session.add(ont)
        db_session.flush()

        result = _ont_has_saved_service_intent(db_session, str(ont.id))
        assert result is False

    def test_returns_true_for_ont_with_wifi_ssid(self, db_session):
        """Should return True when ONT has WiFi SSID configured."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={"wifi": {"ssid": "MyNetwork"}},
        )
        db_session.add(ont)
        db_session.flush()

        result = _ont_has_saved_service_intent(db_session, str(ont.id))
        assert result is True

    def test_returns_true_for_ont_with_wifi_password(self, db_session):
        """Should return True when ONT has WiFi password configured."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={"wifi": {"password": "secret123"}},
        )
        db_session.add(ont)
        db_session.flush()

        result = _ont_has_saved_service_intent(db_session, str(ont.id))
        assert result is True

    def test_returns_true_for_ont_with_pppoe_credentials(self, db_session):
        """Should return True when ONT has PPPoE credentials."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={"wan": {"pppoe_username": "user@isp.com"}},
        )
        db_session.add(ont)
        db_session.flush()

        result = _ont_has_saved_service_intent(db_session, str(ont.id))
        assert result is True

    def test_returns_false_for_ont_with_empty_config(self, db_session):
        """Should return False when ONT has no service config."""
        from app.models.network import OntUnit

        ont = OntUnit(
            id=str(uuid4()),
            serial_number="TEST12345678",
            is_active=True,
            desired_config={},
        )
        db_session.add(ont)
        db_session.flush()

        result = _ont_has_saved_service_intent(db_session, str(ont.id))
        assert result is False
