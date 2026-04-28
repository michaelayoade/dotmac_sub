"""Tests for syslog parser module."""

import pytest

from app.syslog.parsers import (
    HuaweiSyslogParser,
    OntEventType,
    SyslogFacility,
    SyslogSeverity,
)


@pytest.fixture
def parser():
    """Create a parser instance for tests."""
    return HuaweiSyslogParser()


class TestSyslogMessageParsing:
    """Tests for RFC 3164 syslog message parsing."""

    def test_parse_basic_rfc3164_message(self, parser):
        """Test parsing a standard RFC 3164 syslog message."""
        # Priority 134 = facility 16 (local0) * 8 + severity 6 (info)
        data = b"<134>Jan 15 12:34:56 OLT-1 Test message"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")

        assert msg is not None
        assert msg.priority == 134
        assert msg.facility == SyslogFacility.local0
        assert msg.severity == SyslogSeverity.info
        assert msg.hostname == "OLT-1"
        assert msg.message == "Test message"
        assert msg.source_ip == "10.0.0.1"

    def test_parse_without_timestamp(self, parser):
        """Test parsing message without timestamp."""
        data = b"<134>OLT-1 Test message without timestamp"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")

        assert msg is not None
        assert msg.timestamp is None

    def test_parse_without_hostname(self, parser):
        """Test parsing message without explicit hostname."""
        data = b"<134>Jan 15 12:34:56 Test message only"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")

        assert msg is not None
        assert msg.message is not None

    def test_parse_raw_message_without_header(self, parser):
        """Test parsing raw message without RFC 3164 header."""
        data = b"Just a plain message"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")

        assert msg is not None
        assert msg.priority == 134  # Default
        assert msg.facility == SyslogFacility.local7
        assert msg.severity == SyslogSeverity.info

    def test_parse_different_priorities(self, parser):
        """Test parsing messages with different priorities."""
        # local0.error = 16*8 + 3 = 131
        data = b"<131>Jan 1 00:00:00 host error message"
        msg = parser.parse_syslog(data)

        assert msg is not None
        assert msg.facility == SyslogFacility.local0
        assert msg.severity == SyslogSeverity.error

    def test_handle_invalid_utf8(self, parser):
        """Test handling of invalid UTF-8 data."""
        data = b"<134>Jan 15 12:34:56 host Invalid \xff\xfe bytes"
        msg = parser.parse_syslog(data)

        assert msg is not None
        # Should replace invalid bytes


class TestOntAutofindParsing:
    """Tests for ONT AUTOFIND event parsing."""

    def test_parse_autofind_pattern_1(self, parser):
        """Test parsing Huawei ONTAUTOFIND pattern 1."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 %%01GPON/4/ONTAUTOFIND(l)[1]:"
            b"OLT reports an ONT autofind. (OntSn=485754430D3C98EC, Fsp=0/0/0)"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.autofind
        assert event.serial_number == "485754430D3C98EC"
        assert event.frame == 0
        assert event.slot == 0
        assert event.port == 0
        assert event.fsp == "0/0/0"

    def test_parse_autofind_pattern_2(self, parser):
        """Test parsing ONTAUTOFIND with different format."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 GPON ONTAUTOFIND: "
            b"OntSN=HWTC12345678 F/S/P=0/1/2"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.autofind
        assert event.serial_number == "HWTC12345678"
        assert event.frame == 0
        assert event.slot == 1
        assert event.port == 2

    def test_parse_autofind_fsp_first(self, parser):
        """Test parsing ONTAUTOFIND when FSP comes before serial."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 %%01GPON/4/ONTAUTOFIND: "
            b"FSP=0/3/4 detected OntSn=ABCD12345678"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.autofind
        assert event.serial_number == "ABCD12345678"
        assert event.frame == 0
        assert event.slot == 3
        assert event.port == 4

    def test_parse_autofind_case_insensitive(self, parser):
        """Test case insensitivity in ONTAUTOFIND parsing."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 ontautofind "
            b"ontsn=HWTC98765432 fsp=1/2/3"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.autofind
        assert event.serial_number == "HWTC98765432"

    def test_parse_autofind_preserves_source_ip(self, parser):
        """Test that source IP is preserved in event."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 ONTAUTOFIND "
            b"OntSn=HWTC12345678 Fsp=0/0/0"
        )
        msg = parser.parse_syslog(data, source_ip="192.168.1.100")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.source_ip == "192.168.1.100"


class TestOntOnlineOfflineParsing:
    """Tests for ONT online/offline event parsing."""

    def test_parse_ont_online_event(self, parser):
        """Test parsing ONT online event."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 %%01GPON/4/ONT_ONLINE: "
            b"ONT came online Fsp=0/1/2/5"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.online
        assert event.frame == 0
        assert event.slot == 1
        assert event.port == 2
        assert event.ont_id == 5

    def test_parse_ont_offline_event(self, parser):
        """Test parsing ONT offline event."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 %%01GPON/4/ONT_OFFLINE: "
            b"ONT went offline F/S/P=0/1/2/10"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.offline
        assert event.ont_id == 10

    def test_parse_dying_gasp_event(self, parser):
        """Test parsing DYING_GASP event."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 %%01GPON/4/DYING_GASP: "
            b"ONT power failure FSP=0/2/3/7"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.dying_gasp

    def test_parse_los_event(self, parser):
        """Test parsing LOS (Loss of Signal) event."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 %%01GPON/4/ONT_LOS: "
            b"Loss of signal detected Fsp=0/0/1/3"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.event_type == OntEventType.los


class TestNonOntMessages:
    """Tests for messages that are not ONT-related."""

    def test_non_ont_message_returns_none(self, parser):
        """Test that non-ONT messages return None event."""
        data = b"<134>Jan 15 12:34:56 OLT-1 System startup complete"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is None

    def test_partial_match_returns_none(self, parser):
        """Test that partial matches don't produce events."""
        # Has AUTOFIND but no FSP
        data = b"<134>Jan 15 12:34:56 OLT-1 ONTAUTOFIND without location"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is None

    def test_fsp_without_event_type_returns_none(self, parser):
        """Test that FSP alone doesn't produce an event."""
        data = b"<134>Jan 15 12:34:56 OLT-1 Config changed at Fsp=0/1/2"
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is None


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_data(self, parser):
        """Test handling of empty data."""
        msg = parser.parse_syslog(b"", source_ip="10.0.0.1")
        # Should return a message with empty content
        assert msg is not None

    def test_very_long_serial_number(self, parser):
        """Test handling of 16-character serial number."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 ONTAUTOFIND "
            b"OntSn=HWTC123456789ABC Fsp=0/0/0"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.serial_number == "HWTC123456789ABC"

    def test_high_port_numbers(self, parser):
        """Test handling of high F/S/P numbers."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 ONTAUTOFIND "
            b"OntSn=HWTC12345678 Fsp=10/15/7"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.frame == 10
        assert event.slot == 15
        assert event.port == 7
        assert event.fsp == "10/15/7"

    def test_comma_separated_fsp(self, parser):
        """Test handling of comma-separated F/S/P."""
        data = (
            b"<134>Jan 15 12:34:56 OLT-1 ONTAUTOFIND "
            b"OntSn=HWTC12345678 Fsp=0,1,2"
        )
        msg = parser.parse_syslog(data, source_ip="10.0.0.1")
        event = parser.parse_ont_event(msg)

        assert event is not None
        assert event.frame == 0
        assert event.slot == 1
        assert event.port == 2
