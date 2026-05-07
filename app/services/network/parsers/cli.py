"""CLI text parsing and validation helpers for OLT commands."""

from __future__ import annotations

import re

FSP_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{1,3}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]+$")
FSP_PREFIX_RE = re.compile(r"^(?:x?g?pon|epon|port|gei|ge|eth)[-_]?", re.IGNORECASE)

HUAWEI_OPTIONAL_ARG_PROMPT = r"\{[^\r\n{}]*\}\s*:?\s*$"

HUAWEI_ERROR_PATTERNS = (
    "failure",
    "error",
    "% parameter error",
    "% unknown command",
    "command not found",
    "invalid",
    "unrecognized",
    "incomplete command",
    "\u5931\u8d25",  # Chinese: "failure"
    "\u9519\u8bef",  # Chinese: "error"
)

READONLY_COMMAND_PREFIXES = (
    "display",
    "show",
    "dir",
    "pwd",
    "more",
    "ping",
    "tracert",
)

DANGEROUS_COMMAND_PREFIXES = (
    "config",
    "undo",
    "delete",
    "reset",
    "reboot",
    "shutdown",
    "format",
    "copy",
    "startup",
    "save",
    "commit",
    "rollback",
    "system",
    "patch",
    "upgrade",
    "restore",
    "ont add",
    "ont delete",
    "service-port",
    "interface",
)


def normalize_fsp(fsp: str) -> str:
    """Normalize F/S/P by stripping common port name prefixes like ``pon-``."""
    if not fsp:
        return fsp
    return FSP_PREFIX_RE.sub("", fsp.strip())


def validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format is strictly numeric."""
    check_fsp = normalize_fsp(fsp)
    if not FSP_RE.match(check_fsp):
        return False, f"Invalid F/S/P format: {fsp!r} (expected digits/digits/digits)"
    return True, ""


def validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number contains only alphanumeric chars and dashes."""
    if not serial_number or not SERIAL_RE.match(serial_number):
        return False, f"Invalid serial number format: {serial_number!r}"
    return True, ""


def is_error_output(output: str) -> bool:
    """Check if Huawei CLI output indicates an error."""
    lower = output.lower()
    return any(pattern in lower for pattern in HUAWEI_ERROR_PATTERNS)


def needs_huawei_command_confirm(output: str) -> bool:
    """Return true when Huawei CLI is waiting for Enter to accept defaults."""
    return (
        "<cr>" in output.lower()
        or re.search(HUAWEI_OPTIONAL_ARG_PROMPT, output) is not None
    )


def validate_readonly_command(command: str) -> tuple[bool, str]:
    """Validate that a CLI command is read-only."""
    normalized = command.strip().lower()

    for prefix in DANGEROUS_COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            return (
                False,
                f"Command '{prefix}' is not allowed \u2014 only read-only commands permitted",
            )

    for prefix in READONLY_COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            return True, ""

    return (
        False,
        "Command not recognized as read-only \u2014 must start with: "
        f"{', '.join(READONLY_COMMAND_PREFIXES)}",
    )
