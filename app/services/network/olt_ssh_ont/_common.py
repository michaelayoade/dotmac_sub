"""Shared utilities, constants, and dataclasses for OLT SSH ONT operations."""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass

from paramiko.ssh_exception import SSHException

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)

# Delay between characters when using slow send (seconds).
# Some OLT terminals corrupt commands sent too quickly.
# Increased from 0.05 to 0.1 for MA5608T compatibility.
_SLOW_SEND_CHAR_DELAY = 0.1

# Regex patterns for validation
_FSP_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{1,3}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]+$")


@dataclass
class OntIphostConfig:
    """Configuration for a single ONT's IPHOST."""

    fsp: str  # Frame/Slot/Port e.g. "0/1/0"
    ont_id: int
    vlan_id: int
    ip_address: str
    subnet: str = "255.255.255.0"
    gateway: str | None = None  # Derived from IP if not provided
    ip_mode: str = "static"
    priority: int | None = None
    serial_number: str | None = None  # For logging/tracking


@dataclass
class OntIphostResult:
    """Result of configuring a single ONT's IPHOST."""

    fsp: str
    ont_id: int
    success: bool
    message: str
    serial_number: str | None = None


@dataclass
class OntStatusEntry:
    """Status of a single registered ONT on an OLT port."""

    serial_number: str
    run_state: str
    config_state: str
    match_state: str


@dataclass
class RegisteredOntEntry:
    """An ONT serial registered on an OLT."""

    fsp: str
    onu_id: int
    real_serial: str
    run_state: str


@dataclass
class ServicePortDiagnostics:
    """Results from running service port diagnostics on an ONT."""

    ont_run_state: str
    ont_config_state: str
    ont_match_state: str
    ont_online: bool
    gem_ports: list[dict[str, str]]
    service_port_details: list[dict[str, str]]
    raw_outputs: dict[str, str]
    warnings: list[str]


def _send_slow(channel, command: str, char_delay: float = _SLOW_SEND_CHAR_DELAY) -> None:
    """Send command with delays to avoid terminal corruption.

    Some OLT terminals (particularly certain Huawei MA5608T units) have terminal
    processing issues that corrupt commands with spaces when sent at full speed.
    This version sends each word (space-separated) with a delay after each word.

    Args:
        channel: Paramiko SSH channel.
        command: Command string to send (without trailing newline).
        char_delay: Delay in seconds between each word.
    """
    # Split by spaces and send each part with space, adding delay after spaces
    parts = command.split(' ')
    for i, part in enumerate(parts):
        channel.send(part)
        if i < len(parts) - 1:
            # Send space and wait for terminal to process
            channel.send(' ')
            time.sleep(char_delay)
    # Small delay before newline
    time.sleep(char_delay)
    channel.send("\n")


def _validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format is strictly numeric (e.g. '0/2/1')."""
    if not _FSP_RE.match(fsp):
        return False, f"Invalid F/S/P format: {fsp!r} (expected digits/digits/digits)"
    return True, ""


def _validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number contains only alphanumeric chars and dashes."""
    if not serial_number or not _SERIAL_RE.match(serial_number):
        return False, f"Invalid serial number format: {serial_number!r}"
    return True, ""


def _safe_profile_name(name: str | None) -> str:
    """Sanitize a profile name for use in OLT commands."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", str(name or "ACS")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "ACS")[:48]
