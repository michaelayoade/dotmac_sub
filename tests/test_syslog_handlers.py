"""Tests for syslog event handlers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.network import OLTDevice
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

    def test_handle_autofind_event_upserts_candidate(self, sample_olt):
        """Test that autofind event directly upserts candidate."""
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

        with patch(
            "app.syslog.handlers._find_olt_by_ip",
            return_value=str(sample_olt.id),
        ), patch(
            "app.syslog.handlers.db_session_adapter"
        ) as mock_adapter, patch(
            "app.syslog.handlers.upsert_autofind_from_syslog",
            return_value=True,
        ) as mock_upsert:
            # Setup the context manager mock
            mock_adapter.session.return_value.__enter__ = lambda s: None
            mock_adapter.session.return_value.__exit__ = lambda s, *args: None

            handle_ont_event(event)

            mock_upsert.assert_called_once()

    def test_handle_online_event_logs_only(self):
        """Test that online event is logged but doesn't upsert candidate."""
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
            "app.syslog.handlers.upsert_autofind_from_syslog",
        ) as mock_upsert:
            handle_ont_event(event)

            # Should not upsert for online events
            mock_upsert.assert_not_called()

    def test_handle_offline_event_logs_only(self):
        """Test that offline event is logged but doesn't upsert candidate."""
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
            "app.syslog.handlers.upsert_autofind_from_syslog",
        ) as mock_upsert:
            handle_ont_event(event)

            mock_upsert.assert_not_called()

    def test_handle_dying_gasp_event_logs_only(self):
        """Test that dying gasp event is logged but doesn't upsert candidate."""
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
            "app.syslog.handlers.upsert_autofind_from_syslog",
        ) as mock_upsert:
            handle_ont_event(event)

            mock_upsert.assert_not_called()


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
            "app.syslog.handlers._find_olt_by_ip",
        ) as mock_find:
            _handle_autofind_event(event)

            # Should not even try to find OLT without source IP
            mock_find.assert_not_called()

    def test_autofind_without_serial_logs_warning(self):
        """Test that autofind event without serial logs warning."""
        event = OntEvent(
            event_type=OntEventType.autofind,
            frame=0,
            slot=1,
            port=2,
            ont_id=None,
            serial_number=None,  # No serial
            raw_message="test message",
            source_ip="10.0.0.50",
        )

        with patch(
            "app.syslog.handlers._find_olt_by_ip",
        ) as mock_find:
            _handle_autofind_event(event)

            # Should not try to find OLT without serial
            mock_find.assert_not_called()

    def test_autofind_olt_not_found_does_not_upsert(self):
        """Test that autofind for unknown OLT IP doesn't upsert."""
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

        with patch(
            "app.syslog.handlers._find_olt_by_ip",
            return_value=None,  # OLT not found
        ), patch(
            "app.syslog.handlers.upsert_autofind_from_syslog",
        ) as mock_upsert:
            _handle_autofind_event(event)

            # Should not upsert when OLT not found
            mock_upsert.assert_not_called()


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
