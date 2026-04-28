"""Syslog message parsers for Huawei OLT events.

Parses RFC 3164 syslog format and detects ONT-related events
such as ONTAUTOFIND, ONT_ONLINE, and ONT_OFFLINE.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class SyslogSeverity(Enum):
    """Syslog severity levels (RFC 5424)."""

    emergency = 0
    alert = 1
    critical = 2
    error = 3
    warning = 4
    notice = 5
    info = 6
    debug = 7


class SyslogFacility(Enum):
    """Syslog facility codes (RFC 5424)."""

    kern = 0
    user = 1
    mail = 2
    daemon = 3
    auth = 4
    syslog = 5
    lpr = 6
    news = 7
    uucp = 8
    cron = 9
    authpriv = 10
    ftp = 11
    ntp = 12
    audit = 13
    console = 14
    cron2 = 15
    local0 = 16
    local1 = 17
    local2 = 18
    local3 = 19
    local4 = 20
    local5 = 21
    local6 = 22
    local7 = 23


class OntEventType(Enum):
    """Types of ONT-related events from OLT syslog."""

    autofind = "autofind"
    online = "online"
    offline = "offline"
    dying_gasp = "dying_gasp"
    los = "los"  # Loss of Signal
    unknown = "unknown"


@dataclass
class SyslogMessage:
    """Parsed syslog message with metadata."""

    raw: str
    priority: int
    facility: SyslogFacility
    severity: SyslogSeverity
    timestamp: datetime | None
    hostname: str | None
    message: str
    source_ip: str | None = None


@dataclass
class OntEvent:
    """Parsed ONT event from syslog message."""

    event_type: OntEventType
    frame: int
    slot: int
    port: int
    ont_id: int | None
    serial_number: str | None
    raw_message: str
    source_ip: str | None = None

    @property
    def fsp(self) -> str:
        """Return F/S/P notation."""
        return f"{self.frame}/{self.slot}/{self.port}"


# RFC 3164 syslog format: <PRI>TIMESTAMP HOSTNAME MSG
# Example: <134>Jan 15 12:34:56 OLT-1 %%01GPON/4/ONTAUTOFIND...
_RFC3164_PATTERN = re.compile(
    r"^<(?P<pri>\d{1,3})>"
    r"(?P<timestamp>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})?\s*"
    r"(?P<hostname>\S+)?\s*"
    r"(?P<message>.*)$",
    re.DOTALL,
)

# Huawei ONTAUTOFIND message patterns
# Pattern 1: %%01GPON/4/ONTAUTOFIND(l)[1]:OLT reports an ONT autofind. (OntSn=485754430D3C98EC, Fsp=0/0/0)
# Pattern 2: %%01GPON/4/ONTAUTOFIND(l):OLT reports... OntSn=HWTC12345678 F/S/P=0/1/2
_HUAWEI_AUTOFIND_PATTERN = re.compile(
    r"ONTAUTOFIND.*?"
    r"(?:OntSn=|OntSN=|SN=)(?P<serial>\w{8,16})"
    r".*?"
    r"(?:Fsp=|F/S/P=|FSP=)(?P<frame>\d+)[/,](?P<slot>\d+)[/,](?P<port>\d+)",
    re.IGNORECASE,
)

# Alternative pattern where FSP comes first
_HUAWEI_AUTOFIND_ALT_PATTERN = re.compile(
    r"ONTAUTOFIND.*?"
    r"(?:Fsp=|F/S/P=|FSP=)(?P<frame>\d+)[/,](?P<slot>\d+)[/,](?P<port>\d+)"
    r".*?"
    r"(?:OntSn=|OntSN=|SN=)(?P<serial>\w{8,16})",
    re.IGNORECASE,
)

# ONT online/offline patterns
_HUAWEI_ONT_ONLINE_PATTERN = re.compile(
    r"ONT_ONLINE|ONTONLINE|ont\s+online",
    re.IGNORECASE,
)

_HUAWEI_ONT_OFFLINE_PATTERN = re.compile(
    r"ONT_OFFLINE|ONTOFFLINE|ont\s+offline|DYING_GASP|LOS",
    re.IGNORECASE,
)

# General F/S/P extraction pattern for non-autofind events
_FSP_PATTERN = re.compile(
    r"(?:Fsp=|F/S/P=|FSP=|Frame/Slot/Port:?\s*)(?P<frame>\d+)[/,](?P<slot>\d+)[/,](?P<port>\d+)"
    r"(?:[/,](?P<ont_id>\d+))?",
    re.IGNORECASE,
)


class HuaweiSyslogParser:
    """Parser for Huawei OLT syslog messages."""

    def parse_syslog(
        self, data: bytes, source_ip: str | None = None
    ) -> SyslogMessage | None:
        """Parse raw syslog data into a SyslogMessage.

        Args:
            data: Raw UDP packet data
            source_ip: Source IP address of the syslog sender

        Returns:
            Parsed SyslogMessage or None if parsing fails
        """
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            logger.debug("Failed to decode syslog data")
            return None

        match = _RFC3164_PATTERN.match(text)
        if not match:
            # Try to parse as raw message without RFC 3164 header
            return SyslogMessage(
                raw=text,
                priority=134,  # Default to local7.info
                facility=SyslogFacility.local7,
                severity=SyslogSeverity.info,
                timestamp=None,
                hostname=source_ip,
                message=text,
                source_ip=source_ip,
            )

        try:
            priority = int(match.group("pri"))
            facility_num = priority >> 3
            severity_num = priority & 0x07

            facility = (
                SyslogFacility(facility_num)
                if facility_num <= 23
                else SyslogFacility.local7
            )
            severity = SyslogSeverity(severity_num)
        except (ValueError, TypeError):
            facility = SyslogFacility.local7
            severity = SyslogSeverity.info
            priority = 134

        timestamp = None
        ts_str = match.group("timestamp")
        if ts_str:
            try:
                # Parse RFC 3164 timestamp (no year, assume current year)
                current_year = datetime.now().year
                timestamp = datetime.strptime(
                    f"{current_year} {ts_str}", "%Y %b %d %H:%M:%S"
                )
            except ValueError:
                pass

        return SyslogMessage(
            raw=text,
            priority=priority,
            facility=facility,
            severity=severity,
            timestamp=timestamp,
            hostname=match.group("hostname"),
            message=match.group("message") or "",
            source_ip=source_ip,
        )

    def parse_ont_event(self, msg: SyslogMessage) -> OntEvent | None:
        """Extract ONT event from a syslog message.

        Args:
            msg: Parsed syslog message

        Returns:
            OntEvent if an ONT-related event is detected, None otherwise
        """
        text = msg.message

        # Check for ONTAUTOFIND event
        match = _HUAWEI_AUTOFIND_PATTERN.search(text)
        if not match:
            match = _HUAWEI_AUTOFIND_ALT_PATTERN.search(text)

        if match:
            return OntEvent(
                event_type=OntEventType.autofind,
                frame=int(match.group("frame")),
                slot=int(match.group("slot")),
                port=int(match.group("port")),
                ont_id=None,
                serial_number=match.group("serial"),
                raw_message=msg.raw,
                source_ip=msg.source_ip,
            )

        # Check for online/offline events
        fsp_match = _FSP_PATTERN.search(text)
        if not fsp_match:
            return None

        frame = int(fsp_match.group("frame"))
        slot = int(fsp_match.group("slot"))
        port = int(fsp_match.group("port"))
        ont_id_str = fsp_match.group("ont_id")
        ont_id = int(ont_id_str) if ont_id_str else None

        if _HUAWEI_ONT_ONLINE_PATTERN.search(text):
            event_type = OntEventType.online
        elif "DYING_GASP" in text.upper():
            event_type = OntEventType.dying_gasp
        elif "LOS" in text.upper() and "ONT" in text.upper():
            event_type = OntEventType.los
        elif _HUAWEI_ONT_OFFLINE_PATTERN.search(text):
            event_type = OntEventType.offline
        else:
            return None

        return OntEvent(
            event_type=event_type,
            frame=frame,
            slot=slot,
            port=port,
            ont_id=ont_id,
            serial_number=None,
            raw_message=msg.raw,
            source_ip=msg.source_ip,
        )


# Singleton instance
huawei_parser = HuaweiSyslogParser()
