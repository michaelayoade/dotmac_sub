"""TextFSM template loader and parsing utilities for OLT CLI output.

This module provides structured parsing of Huawei OLT CLI output using TextFSM
templates. Templates are loaded from the templates/ subdirectory and cached
for performance.

Usage:
    from app.services.network.parsers import parse_autofind, parse_service_port_table

    entries = parse_autofind(raw_output, vendor="huawei")
    ports = parse_service_port_table(raw_output, vendor="huawei")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Generic, TypeVar

import textfsm

logger = logging.getLogger(__name__)

T = TypeVar("T")

TEMPLATES_DIR = Path(__file__).parent / "templates"


class ParseError(Exception):
    """Raised when parsing fails due to invalid output or template mismatch."""

    pass


@dataclass
class ParseResult(Generic[T]):
    """Result of a parsing operation with metadata."""

    success: bool
    data: list[T]
    raw_output: str
    template_name: str
    warnings: list[str] = field(default_factory=list)
    row_count: int = 0

    @property
    def confidence(self) -> float:
        """Estimate parsing confidence based on row extraction rate."""
        if not self.raw_output.strip():
            return 0.0
        # Count expected record markers in output
        markers = self.raw_output.count("---") + self.raw_output.count("F/S/P")
        if markers == 0:
            return 1.0 if self.row_count > 0 else 0.0
        return min(1.0, self.row_count / max(markers, 1))


@lru_cache(maxsize=32)
def _load_template(template_name: str, vendor: str = "huawei") -> textfsm.TextFSM:
    """Load and cache a TextFSM template.

    Args:
        template_name: Name of the template file (without .textfsm extension).
        vendor: Vendor subdirectory (default: huawei).

    Returns:
        Compiled TextFSM template object.

    Raises:
        ParseError: If template file not found or invalid.
    """
    template_path = TEMPLATES_DIR / vendor / f"{template_name}.textfsm"
    if not template_path.exists():
        raise ParseError(f"Template not found: {template_path}")

    try:
        with open(template_path) as f:
            return textfsm.TextFSM(f)
    except textfsm.TextFSMTemplateError as e:
        raise ParseError(f"Invalid template {template_name}: {e}") from e


def _parse_with_template(
    output: str,
    template_name: str,
    vendor: str = "huawei",
) -> tuple[list[str], list[list[Any]]]:
    """Parse output using a TextFSM template.

    Args:
        output: Raw CLI output to parse.
        template_name: Name of the template to use.
        vendor: Vendor subdirectory.

    Returns:
        Tuple of (header names, list of row values).
    """
    template = _load_template(template_name, vendor)
    # Reset template state for reuse (templates are cached)
    template.Reset()

    try:
        results = template.ParseText(output)
    except textfsm.TextFSMError as e:
        logger.warning("TextFSM parse error for %s: %s", template_name, e)
        return [], []

    return template.header, results


# ---------------------------------------------------------------------------
# Autofind parser
# ---------------------------------------------------------------------------


@dataclass
class AutofindEntry:
    """A single ONT discovered via Huawei autofind."""

    fsp: str
    serial_number: str
    serial_hex: str
    vendor_id: str
    model: str
    software_version: str
    mac: str
    equipment_sn: str
    autofind_time: str


def parse_autofind(
    output: str,
    vendor: str = "huawei",
) -> ParseResult[AutofindEntry]:
    """Parse `display ont autofind all` output.

    Args:
        output: Raw CLI output from the autofind command.
        vendor: OLT vendor (default: huawei).

    Returns:
        ParseResult containing list of AutofindEntry objects.
    """
    template_name = "display_ont_autofind"
    warnings: list[str] = []

    # Validate output looks like autofind
    if not output.strip():
        return ParseResult(
            success=True,
            data=[],
            raw_output=output,
            template_name=template_name,
            warnings=["Empty output"],
            row_count=0,
        )

    # Check for expected markers
    has_markers = "F/S/P" in output or "Ont SN" in output or "Number" in output
    if not has_markers:
        warnings.append("Output may not be autofind format - missing expected markers")

    headers, rows = _parse_with_template(output, template_name, vendor)

    entries: list[AutofindEntry] = []
    for row in rows:
        try:
            row_dict = dict(zip(headers, row))
            entries.append(
                AutofindEntry(
                    fsp=row_dict.get("FSP", ""),
                    serial_number=row_dict.get("SERIAL_DISPLAY", ""),
                    serial_hex=row_dict.get("SERIAL_HEX", ""),
                    vendor_id=row_dict.get("VENDOR_ID", ""),
                    model=row_dict.get("EQUIPMENT_ID", ""),
                    software_version=row_dict.get("SOFTWARE_VERSION", ""),
                    mac=row_dict.get("MAC", ""),
                    equipment_sn=row_dict.get("EQUIPMENT_SN", ""),
                    autofind_time=row_dict.get("AUTOFIND_TIME", ""),
                )
            )
        except (KeyError, IndexError) as e:
            warnings.append(f"Row parse error: {e}")

    return ParseResult(
        success=True,
        data=entries,
        raw_output=output,
        template_name=template_name,
        warnings=warnings,
        row_count=len(entries),
    )


# ---------------------------------------------------------------------------
# Service port parser
# ---------------------------------------------------------------------------


@dataclass
class ServicePortEntry:
    """A single L2 service-port binding on a Huawei OLT."""

    index: int
    vlan_id: int
    ont_id: int
    gem_index: int
    flow_type: str
    flow_para: str
    state: str
    fsp: str = ""
    tag_transform: str = ""


def parse_service_port_table(
    output: str,
    vendor: str = "huawei",
) -> ParseResult[ServicePortEntry]:
    """Parse `display service-port` output.

    Args:
        output: Raw CLI output from service-port display.
        vendor: OLT vendor (default: huawei).

    Returns:
        ParseResult containing list of ServicePortEntry objects.
    """
    template_name = "display_service_port"
    warnings: list[str] = []

    if not output.strip():
        return ParseResult(
            success=True,
            data=[],
            raw_output=output,
            template_name=template_name,
            warnings=["Empty output"],
            row_count=0,
        )

    headers, rows = _parse_with_template(output, template_name, vendor)

    entries: list[ServicePortEntry] = []
    for row in rows:
        try:
            row_dict = dict(zip(headers, row))

            # Parse numeric fields safely
            index = _safe_int(row_dict.get("INDEX", "0"))
            vlan_id = _safe_int(row_dict.get("VLAN_ID", "0"))
            ont_id = _safe_int(row_dict.get("ONT_ID", "0"))
            gem_index = _safe_int(row_dict.get("GEM_INDEX", "0"))

            entries.append(
                ServicePortEntry(
                    index=index,
                    vlan_id=vlan_id,
                    ont_id=ont_id,
                    gem_index=gem_index,
                    flow_type=row_dict.get("FLOW_TYPE", ""),
                    flow_para=row_dict.get("FLOW_PARA", ""),
                    state=row_dict.get("STATE", "").lower(),
                    fsp=row_dict.get("FSP", ""),
                    tag_transform=row_dict.get("TAG_TRANSFORM", ""),
                )
            )
        except (KeyError, IndexError) as e:
            warnings.append(f"Row parse error: {e}")

    return ParseResult(
        success=True,
        data=entries,
        raw_output=output,
        template_name=template_name,
        warnings=warnings,
        row_count=len(entries),
    )


# ---------------------------------------------------------------------------
# Profile table parser
# ---------------------------------------------------------------------------


@dataclass
class ProfileEntry:
    """A single OLT profile entry (line, service, TR-069, or WAN)."""

    profile_id: int
    name: str
    type: str = ""
    binding_count: int = 0
    extra: dict[str, str] = field(default_factory=dict)


def parse_profile_table(
    output: str,
    vendor: str = "huawei",
) -> ParseResult[ProfileEntry]:
    """Parse profile listing output (line, service, or TR-069 profiles).

    Args:
        output: Raw CLI output from profile display command.
        vendor: OLT vendor (default: huawei).

    Returns:
        ParseResult containing list of ProfileEntry objects.
    """
    template_name = "display_ont_profile"
    warnings: list[str] = []

    if not output.strip():
        return ParseResult(
            success=True,
            data=[],
            raw_output=output,
            template_name=template_name,
            warnings=["Empty output"],
            row_count=0,
        )

    headers, rows = _parse_with_template(output, template_name, vendor)

    entries: list[ProfileEntry] = []
    for row in rows:
        try:
            row_dict = dict(zip(headers, row))
            entries.append(
                ProfileEntry(
                    profile_id=_safe_int(row_dict.get("PROFILE_ID", "0")),
                    name=row_dict.get("NAME", ""),
                    type=row_dict.get("TYPE", ""),
                    binding_count=_safe_int(row_dict.get("BINDING_COUNT", "0")),
                )
            )
        except (KeyError, IndexError) as e:
            warnings.append(f"Row parse error: {e}")

    return ParseResult(
        success=True,
        data=entries,
        raw_output=output,
        template_name=template_name,
        warnings=warnings,
        row_count=len(entries),
    )


# ---------------------------------------------------------------------------
# Key-value parser (for detail views)
# ---------------------------------------------------------------------------


def parse_key_value(output: str) -> dict[str, str]:
    """Parse key-value output like TR-069 profile detail.

    Handles various formats:
    - "Key : Value"
    - "Key: Value"
    - "Key            : Value"

    Args:
        output: Raw CLI output with key-value pairs.

    Returns:
        Dictionary of normalized keys to values.
    """
    result: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        # Handle "Key : Value" with optional spaces around colon
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and not key.startswith("-"):  # Skip separator lines
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# ONT info parser (display ont info)
# ---------------------------------------------------------------------------


@dataclass
class OntInfoEntry:
    """Detailed ONT information from display ont info."""

    fsp: str
    ont_id: int
    serial_number: str
    control_flag: str
    run_state: str
    config_state: str
    match_state: str
    description: str = ""
    vendor_id: str = ""
    model: str = ""
    software_version: str = ""


def parse_ont_info(
    output: str,
    vendor: str = "huawei",
) -> ParseResult[OntInfoEntry]:
    """Parse `display ont info` output.

    Args:
        output: Raw CLI output from ont info display.
        vendor: OLT vendor (default: huawei).

    Returns:
        ParseResult containing list of OntInfoEntry objects.
    """
    template_name = "display_ont_info"
    warnings: list[str] = []

    if not output.strip():
        return ParseResult(
            success=True,
            data=[],
            raw_output=output,
            template_name=template_name,
            warnings=["Empty output"],
            row_count=0,
        )

    headers, rows = _parse_with_template(output, template_name, vendor)

    entries: list[OntInfoEntry] = []
    for row in rows:
        try:
            row_dict = dict(zip(headers, row))
            entries.append(
                OntInfoEntry(
                    fsp=row_dict.get("FSP", ""),
                    ont_id=_safe_int(row_dict.get("ONT_ID", "0")),
                    serial_number=row_dict.get("SERIAL_NUMBER", ""),
                    control_flag=row_dict.get("CONTROL_FLAG", ""),
                    run_state=row_dict.get("RUN_STATE", ""),
                    config_state=row_dict.get("CONFIG_STATE", ""),
                    match_state=row_dict.get("MATCH_STATE", ""),
                    description=row_dict.get("DESCRIPTION", ""),
                )
            )
        except (KeyError, IndexError) as e:
            warnings.append(f"Row parse error: {e}")

    return ParseResult(
        success=True,
        data=entries,
        raw_output=output,
        template_name=template_name,
        warnings=warnings,
        row_count=len(entries),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_int(value: str, default: int = 0) -> int:
    """Safely convert string to int."""
    try:
        # Handle values like "27" or "(27)"
        match = re.search(r"(\d+)", str(value))
        return int(match.group(1)) if match else default
    except (ValueError, AttributeError):
        return default


def clear_template_cache() -> None:
    """Clear the template cache (useful for testing/reloading)."""
    _load_template.cache_clear()
