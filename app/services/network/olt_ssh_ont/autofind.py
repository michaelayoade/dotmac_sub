"""ONT autofind query functions via OLT SSH."""

from __future__ import annotations

from app.models.network import OLTDevice
from app.services.network.olt_ssh_session import OltSession, olt_session
from app.services.network.olt_validators import validate_fsp
from app.services.network.parsers.loader import AutofindEntry, parse_autofind


def build_autofind_command(port: str | None = None) -> str:
    """Build a Huawei autofind display command."""
    if port:
        return f"display ont autofind {validate_fsp(port)}"
    return "display ont autofind all"


def parse_autofind_output(output: str) -> list[AutofindEntry]:
    """Parse Huawei autofind output into typed entries."""
    result = parse_autofind(output, vendor="huawei")
    return list(result.data)


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
        raise RuntimeError(
            result.message or result.output or "OLT autofind query failed"
        )
    return parse_autofind_output(result.output)


def query_ont_autofind(
    olt: OLTDevice,
    port: str | None = None,
) -> tuple[bool, str, list[AutofindEntry]]:
    """Query undiscovered ONTs from a Huawei OLT."""
    try:
        with olt_session(olt) as session:
            entries = query_ont_autofind_session(session, port=port)
        noun = "entry" if len(entries) == 1 else "entries"
        return True, f"Found {len(entries)} autofind {noun}", entries
    except Exception as exc:
        return False, f"Autofind query failed: {exc}", []
