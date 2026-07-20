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
    program_areas: dict[str, str] = {}
    current_program_area: str | None = None

    for line in output.splitlines():
        line_lower = line.lower()

        top_level_version = re.match(
            r"^\s*(?:software\s+)?version\s*:\s*(\S+)",
            line,
            re.IGNORECASE,
        )
        if top_level_version:
            info.current_version = top_level_version.group(1)
        elif "version" in line_lower and "software" in line_lower:
            legacy_version = re.search(r"Version(?:\s*:)?\s+(\S+)", line, re.IGNORECASE)
            if legacy_version:
                info.current_version = legacy_version.group(1)

        area_version = re.search(
            r"Program\s+Area\s+([AB])\s+Version\s*:\s*(\S+)",
            line,
            re.IGNORECASE,
        )
        if area_version:
            program_areas[area_version.group(1).upper()] = area_version.group(2)

        area_marker = re.search(
            r"Current\s+Program\s+Area\s*:\s*([AB])\b", line, re.IGNORECASE
        )
        if area_marker:
            current_program_area = area_marker.group(1).upper()

        if "uptime" in line_lower:
            match = re.search(r"uptime(?:\s+is|\s*:)?\s+(.+)$", line, re.IGNORECASE)
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

    if program_areas:
        info.has_dual_image = len(program_areas) > 1
        if current_program_area is None and info.current_version:
            matching_areas = [
                area
                for area, version in program_areas.items()
                if version == info.current_version
            ]
            if matching_areas:
                current_program_area = matching_areas[0]
        if not info.current_version and current_program_area:
            info.current_version = program_areas.get(current_program_area)
        standby_area = "B" if current_program_area == "A" else "A"
        info.standby_version = program_areas.get(standby_area)
        if not info.current_version:
            info.current_version = program_areas.get("A") or program_areas.get("B")
            if info.current_version == info.standby_version:
                info.standby_version = None

    return info
