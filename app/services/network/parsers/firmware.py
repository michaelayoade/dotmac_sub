"""Parsers for OLT firmware/version command output."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FirmwareInfo:
    """Firmware version information from OLT."""

    current_version: str | None = None
    standby_version: str | None = None
    running_board: str | None = None
    standby_board: str | None = None
    uptime: str | None = None
    has_dual_image: bool = False


def parse_firmware_info(output: str) -> FirmwareInfo:
    """Parse firmware version information from ``display version`` output."""
    info = FirmwareInfo()

    for line in output.splitlines():
        line_lower = line.lower()

        if "version" in line_lower and "software" in line_lower:
            match = re.search(r"Version\s+(\S+)", line, re.IGNORECASE)
            if match:
                info.current_version = match.group(1)

        if "uptime" in line_lower:
            match = re.search(r"uptime[:\s]+(.+)$", line, re.IGNORECASE)
            if match:
                info.uptime = match.group(1).strip()

        if "board" in line_lower and ("main" in line_lower or "master" in line_lower):
            info.running_board = line.strip()
        if "board" in line_lower and ("standby" in line_lower or "slave" in line_lower):
            info.standby_board = line.strip()
            info.has_dual_image = True

        if "standby" in line_lower and "version" in line_lower:
            match = re.search(r"Version[:\s]+(\S+)", line, re.IGNORECASE)
            if match:
                info.standby_version = match.group(1)
                info.has_dual_image = True

    return info
