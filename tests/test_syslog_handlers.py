"""Tests for syslog event handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.models.network import OLTDevice
from app.services.autofind_trigger import AutofindTriggerResult
from app.syslog.handlers import _handle_autofind_event, handle_ont_event
from app.syslog.parsers import OntEvent, OntEventType


@pytest.fixture
def sample_olt(db_session):
    """Create a sample OLT for testing."""
    olt = OLTDevice(
        name="Syslog-Handler-OLT",
        mgmt_ip="10.0.0.50",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    return olt


class TestHandleOntEvent:
    """Tests for handle_ont_event function."""

    def test_handle_autofind_event_triggers_autofind(self, sample_olt):
        """Test that autofind event triggers autofind scan."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=1,
            port=2,
            ont_id=None,
            serial_number="HWTC12345678",
            raw_message="test message",
            source_ip="10.0.0.50",
        )

        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Syslog-Handler-OLT",
            task_id="task-syslog-123",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
            return_value=mock_result,
        ) as mock_trigger:
            handle_ont_event(event)

            mock_trigger.assert_called_once()
            call_args = mock_trigger.call_args
            assert call_args[1]["ip_address"] == "10.0.0.50"
            assert call_args[1]["source"] == "syslog"

    def test_handle_online_event_logs_only(self):
        """Test that online event is logged but doesn't trigger autofind."""
        event = OntEvent(
            event_type=OntEventType.online,
            frame=0,
            slot=1,
            port=2,
            ont_id=5,
            serial_number=None,
            raw_message="test message",
            source_ip="10.0.0.50",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
        ) as mock_trigger:
            handle_ont_event(event)

            # Should not trigger autofind for online events
            mock_trigger.assert_not_called()

    def test_handle_offline_event_logs_only(self):
        """Test that offline event is logged but doesn't trigger autofind."""
        event = OntEvent(
            event_type=OntEventType.offline,
            frame=0,
            slot=1,
            port=2,
            ont_id=5,
            serial_number=None,
            raw_message="test message",
            source_ip="10.0.0.50",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
        ) as mock_trigger:
            handle_ont_event(event)

            mock_trigger.assert_not_called()

    def test_handle_dying_gasp_event_logs_only(self):
        """Test that dying gasp event is logged but doesn't trigger autofind."""
        event = OntEvent(
            event_type=OntEventType.dying_gasp,
            frame=0,
            slot=1,
            port=2,
            ont_id=3,
            serial_number=None,
            raw_message="test message",
            source_ip="10.0.0.50",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
        ) as mock_trigger:
            handle_ont_event(event)

            mock_trigger.assert_not_called()


class TestHandleAutofindEvent:
    """Tests for _handle_autofind_event function."""

    def test_autofind_without_source_ip_logs_warning(self):
        """Test that autofind event without source IP logs warning."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=1,
            port=2,
            ont_id=None,
            serial_number="HWTC12345678",
            raw_message="test message",
            source_ip=None,  # No source IP
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
        ) as mock_trigger:
            _handle_autofind_event(event)

            # Should not trigger autofind without source IP
            mock_trigger.assert_not_called()

    def test_autofind_triggers_with_correct_parameters(self, sample_olt):
        """Test that autofind is triggered with correct parameters."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=3,
            port=7,
            ont_id=None,
            serial_number="ABCD98765432",
            raw_message="<134>Jan 1 00:00:00 OLT ONTAUTOFIND...",
            source_ip="10.0.0.50",
        )

        mock_result = AutofindTriggerResult(
            triggered=True,
            olt_id=str(sample_olt.id),
            olt_name="Syslog-Handler-OLT",
            task_id="task-params-123",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
            return_value=mock_result,
        ) as mock_trigger:
            _handle_autofind_event(event)

            mock_trigger.assert_called_once()
            call_kwargs = mock_trigger.call_args[1]
            assert call_kwargs["ip_address"] == "10.0.0.50"
            assert call_kwargs["source"] == "syslog"

    def test_autofind_skipped_when_in_cooldown(self, sample_olt):
        """Test that autofind is skipped when OLT is in cooldown."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=1,
            port=2,
            ont_id=None,
            serial_number="HWTC12345678",
            raw_message="test message",
            source_ip="10.0.0.50",
        )

        mock_result = AutofindTriggerResult(
            triggered=False,
            olt_id=str(sample_olt.id),
            olt_name="Syslog-Handler-OLT",
            reason="OLT is in cooldown period",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
            return_value=mock_result,
        ):
            # Should not raise, just log
            _handle_autofind_event(event)

    def test_autofind_olt_not_found(self):
        """Test handling when OLT is not found for IP."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=1,
            port=2,
            ont_id=None,
            serial_number="HWTC12345678",
            raw_message="test message",
            source_ip="192.168.99.99",  # Unknown IP
        )

        mock_result = AutofindTriggerResult(
            triggered=False,
            reason="No active OLT found with IP 192.168.99.99",
        )

        with patch(
            "app.syslog.handlers.trigger_autofind_by_ip",
            return_value=mock_result,
        ):
            # Should not raise, just log
            _handle_autofind_event(event)


class TestOntEventFspProperty:
    """Tests for OntEvent.fsp property."""

    def test_fsp_format(self):
        """Test that FSP property returns correct format."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=1,
            slot=2,
            port=3,
            ont_id=None,
            serial_number="TEST",
            raw_message="test",
        )

        assert event.fsp == "1/2/3"

    def test_fsp_zero_values(self):
        """Test FSP with zero values."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=0,
            port=0,
            ont_id=None,
            serial_number="TEST",
            raw_message="test",
        )

        assert event.fsp == "0/0/0"

    def test_fsp_high_values(self):
        """Test FSP with high values."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=10,
            slot=15,
            port=7,
            ont_id=None,
            serial_number="TEST",
            raw_message="test",
        )

        assert event.fsp == "10/15/7"
