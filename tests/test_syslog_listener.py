"""Tests for syslog listener module."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

from app.syslog.listener import SyslogListener, SyslogProtocol


def _run_async(coro):
    # Drive coroutines in a dedicated thread to avoid nested event loops
    # interacting with anyio / other test harnesses in the broader suite.
    # Matches the convention used by the rest of the project's async tests.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


class TestSyslogProtocol:
    """Tests for SyslogProtocol class."""

    def test_datagram_received_calls_callback(self):
        """Test that datagram_received creates task with callback."""
        callback = AsyncMock()
        protocol = SyslogProtocol(callback)

        data = b"<134>Jan 1 00:00:00 host test message"
        addr = ("10.0.0.1", 514)

        async def _drive():
            protocol.datagram_received(data, addr)
            # Give the task a chance to run
            await asyncio.sleep(0.01)

        _run_async(_drive())

        callback.assert_called_once_with(data, addr)

    def test_connection_made_stores_transport(self):
        """Test that connection_made stores transport."""
        protocol = SyslogProtocol(AsyncMock())
        transport = MagicMock()

        protocol.connection_made(transport)

        assert protocol.transport is transport


class TestSyslogListener:
    """Tests for SyslogListener class."""

    def test_init_default_values(self):
        """Test listener initialization with defaults."""
        listener = SyslogListener()

        assert listener.host == "0.0.0.0"
        assert listener.port == 514
        assert listener._running is False
        assert listener._event_count == 0

    def test_init_custom_values(self):
        """Test listener initialization with custom values."""
        listener = SyslogListener(host="127.0.0.1", port=5514)

        assert listener.host == "127.0.0.1"
        assert listener.port == 5514

    def test_stop_sets_running_false(self):
        """Test that stop() sets _running to False."""
        listener = SyslogListener()
        listener._running = True

        _run_async(listener.stop())

        assert listener._running is False

    def test_stop_closes_transport(self):
        """Test that stop() closes transport."""
        listener = SyslogListener()
        listener._running = True
        mock_transport = MagicMock()
        listener._transport = mock_transport

        _run_async(listener.stop())

        mock_transport.close.assert_called_once()
        assert listener._transport is None

    def test_on_message_received_increments_count(self):
        """Test that message handling increments event count."""
        listener = SyslogListener()
        listener._event_count = 0

        with patch("app.syslog.listener.huawei_parser") as mock_parser:
            mock_parser.parse_syslog.return_value = MagicMock(message="test")
            mock_parser.parse_ont_event.return_value = None

            _run_async(listener._on_message_received(b"<134>test", ("10.0.0.1", 514)))

        assert listener._event_count == 1

    def test_on_message_received_parses_and_handles_event(self):
        """Test that valid ONT event is parsed and handled."""
        listener = SyslogListener()
        listener._ont_event_count = 0

        mock_msg = MagicMock()
        mock_event = MagicMock()

        with (
            patch("app.syslog.listener.huawei_parser") as mock_parser,
            patch("app.syslog.listener.handle_ont_event") as mock_handler,
        ):
            mock_parser.parse_syslog.return_value = mock_msg
            mock_parser.parse_ont_event.return_value = mock_event

            _run_async(
                listener._on_message_received(
                    b"<134>ONTAUTOFIND OntSn=HWTC12345678 Fsp=0/0/0",
                    ("10.0.0.1", 514),
                )
            )

        assert listener._ont_event_count == 1
        mock_handler.assert_called_once_with(mock_event)

    def test_on_message_received_handles_parse_failure(self):
        """Test that parse failure is handled gracefully."""
        listener = SyslogListener()

        with patch("app.syslog.listener.huawei_parser") as mock_parser:
            mock_parser.parse_syslog.return_value = None

            # Should not raise
            _run_async(listener._on_message_received(b"invalid", ("10.0.0.1", 514)))

        assert listener._event_count == 1
        assert listener._ont_event_count == 0

    def test_on_message_received_handles_exception(self):
        """Test that exceptions during processing are caught."""
        listener = SyslogListener()

        with patch("app.syslog.listener.huawei_parser") as mock_parser:
            mock_parser.parse_syslog.side_effect = Exception("Parse error")

            # Should not raise
            _run_async(listener._on_message_received(b"test", ("10.0.0.1", 514)))

        assert listener._event_count == 1

    def test_run_when_disabled(self):
        """Test that run() returns immediately when disabled."""
        listener = SyslogListener()

        with patch("app.syslog.listener.SYSLOG_ENABLED", False):
            _run_async(listener.run())

        assert listener._running is False

    def test_maybe_log_stats_logs_periodically(self):
        """Test that stats are logged every 100 events."""
        from datetime import UTC, datetime

        listener = SyslogListener()
        listener._start_time = datetime.now(UTC)
        listener._event_count = 100
        listener._last_stats_log_count = 0

        with patch("app.syslog.listener.logger") as mock_logger:
            listener._maybe_log_stats()

            mock_logger.info.assert_called()
            assert listener._last_stats_log_count == 100

    def test_maybe_log_stats_skips_when_not_enough_events(self):
        """Test that stats are not logged before 100 events."""
        from datetime import UTC, datetime

        listener = SyslogListener()
        listener._start_time = datetime.now(UTC)
        listener._event_count = 50
        listener._last_stats_log_count = 0

        with patch("app.syslog.listener.logger") as mock_logger:
            listener._maybe_log_stats()

            mock_logger.info.assert_not_called()


class TestSyslogListenerIntegration:
    """Integration tests for syslog listener."""

    def test_full_message_flow(self):
        """Test complete flow from UDP receive to event handling."""
        listener = SyslogListener(port=15514)  # Use non-privileged port

        # Mock the handler to track calls
        handled_events = []

        def capture_event(event):
            handled_events.append(event)

        # Simulate a complete message flow
        with patch("app.syslog.listener.handle_ont_event", side_effect=capture_event):
            data = b"<134>Jan 1 00:00:00 OLT %%01GPON/4/ONTAUTOFIND: OntSn=HWTC12345678 Fsp=0/1/2"
            _run_async(listener._on_message_received(data, ("10.0.0.100", 514)))

        assert len(handled_events) == 1
        event = handled_events[0]
        assert event.serial_number == "HWTC12345678"
        assert event.frame == 0
        assert event.slot == 1
        assert event.port == 2
        assert event.source_ip == "10.0.0.100"
