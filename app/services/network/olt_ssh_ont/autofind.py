"""ONT autofind query functions via OLT SSH."""

from __future__ import annotations

import logging

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    is_huawei_no_autofind_entries,
)
from app.services.network.huawei_command_profiles import get_huawei_command_profile
from app.services.network.olt_ssh_session import OltSession, olt_session
from app.services.network.olt_validators import validate_fsp
from app.services.network.parsers.loader import AutofindEntry, parse_autofind

logger = logging.getLogger(__name__)


class AutofindCommandError(RuntimeError):
    """Typed Huawei autofind command rejection retained across session retries."""

    def __init__(self, message: str, *, error_code: HuaweiCliErrorCode) -> None:
        super().__init__(message)
        self.error_code = error_code


def build_autofind_command(port: str | None = None) -> str:
    """Build a Huawei autofind display command."""
    if port:
        return f"display ont autofind {validate_fsp(port)}"
    return "display ont autofind all"


def parse_autofind_output(output: str) -> list[AutofindEntry]:
    """Parse Huawei autofind output into typed entries."""
    result = parse_autofind(output, vendor="huawei")
    return list(result.data)


def _is_no_autofind_entries_output(output: str) -> bool:
    return is_huawei_no_autofind_entries(output)


def query_ont_autofind_session(
    session: OltSession,
    port: str | None = None,
) -> list[AutofindEntry]:
    """Query undiscovered ONTs through an existing OLT SSH session."""
    result = session.run_command(
        build_autofind_command(port),
        timeout_sec=20,
        slow_send=False,
    )
    if not result.success:
        if _is_no_autofind_entries_output(result.output):
            return []
        raise AutofindCommandError(
            result.message or result.output or "OLT autofind query failed",
            error_code=result.error_code,
        )
    return parse_autofind_output(result.output)


def _query_in_fresh_session(
    olt: OLTDevice,
    *,
    port: str | None,
) -> list[AutofindEntry]:
    with olt_session(olt) as session:
        return query_ont_autofind_session(session, port=port)


def _query_global_with_bounded_retry(olt: OLTDevice) -> list[AutofindEntry]:
    """Read global autofind, retrying one transient false grammar rejection."""

    try:
        return _query_in_fresh_session(olt, port=None)
    except AutofindCommandError as exc:
        if exc.error_code is not HuaweiCliErrorCode.UNKNOWN_COMMAND:
            raise
        logger.warning(
            "Global Huawei autofind was rejected once on OLT %s; retrying in a "
            "fresh read-only session",
            getattr(olt, "name", "unknown"),
        )
        return _query_in_fresh_session(olt, port=None)


def query_ont_autofind(
    olt: OLTDevice,
    port: str | None = None,
) -> tuple[bool, str, list[AutofindEntry]]:
    """Query undiscovered ONTs from a Huawei OLT."""
    try:
        requested_fsp = validate_fsp(port) if port else None
        profile = get_huawei_command_profile(olt)
        command_fsp = (
            requested_fsp
            if requested_fsp is not None and profile.supports_scoped_autofind
            else None
        )
        if command_fsp is None:
            entries = _query_global_with_bounded_retry(olt)
        else:
            try:
                entries = _query_in_fresh_session(olt, port=command_fsp)
            except AutofindCommandError as exc:
                if exc.error_code is not HuaweiCliErrorCode.UNKNOWN_COMMAND:
                    raise
                logger.warning(
                    "Scoped Huawei autofind is unsupported on OLT %s; falling "
                    "back to global inventory with exact in-process filtering",
                    getattr(olt, "name", "unknown"),
                )
                entries = _query_global_with_bounded_retry(olt)
        if requested_fsp is not None:
            entries = [
                entry
                for entry in entries
                if str(entry.fsp or "").strip() == requested_fsp
            ]
        noun = "entry" if len(entries) == 1 else "entries"
        return True, f"Found {len(entries)} autofind {noun}", entries
    except Exception as exc:
        return False, f"Autofind query failed: {exc}", []
