"""Diagnostic actions for CPE devices."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    get_cpe_or_error,
    resolve_cpe_client_or_error,
)

logger = logging.getLogger(__name__)


def run_ping_diagnostic(
    db: Session, cpe_id: str, host: str, count: int = 4
) -> ActionResult:
    """Run a ping diagnostic from the CPE device via TR-069."""
    if not host or not host.strip():
        return ActionResult(success=False, message="Ping target host is required.")

    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    count = max(1, min(count, 20))
    params = {
        "Device.IP.Diagnostics.IPPing.Host": host.strip(),
        "Device.IP.Diagnostics.IPPing.NumberOfRepetitions": str(count),
        "Device.IP.Diagnostics.IPPing.DiagnosticsState": "Requested",
        "InternetGatewayDevice.IPPingDiagnostics.Host": host.strip(),
        "InternetGatewayDevice.IPPingDiagnostics.NumberOfRepetitions": str(count),
        "InternetGatewayDevice.IPPingDiagnostics.DiagnosticsState": "Requested",
    }
    try:
        result = client.set_parameter_values(device_id, params)
        logger.info(
            "Ping diagnostic started on CPE %s → %s (%d pings)",
            cpe.serial_number,
            host.strip(),
            count,
        )
        return ActionResult(
            success=True,
            message=(
                f"Ping diagnostic started on {cpe.serial_number} → {host.strip()} "
                f"({count} pings). Results will appear after the next device inform."
            ),
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Ping diagnostic failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(
            success=False, message=f"Failed to start ping diagnostic: {exc}"
        )


def run_traceroute_diagnostic(db: Session, cpe_id: str, host: str) -> ActionResult:
    """Run a traceroute diagnostic from the CPE device via TR-069."""
    if not host or not host.strip():
        return ActionResult(
            success=False, message="Traceroute target host is required."
        )

    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    params = {
        "Device.IP.Diagnostics.TraceRoute.Host": host.strip(),
        "Device.IP.Diagnostics.TraceRoute.DiagnosticsState": "Requested",
        "InternetGatewayDevice.TraceRouteDiagnostics.Host": host.strip(),
        "InternetGatewayDevice.TraceRouteDiagnostics.DiagnosticsState": "Requested",
    }
    try:
        result = client.set_parameter_values(device_id, params)
        logger.info(
            "Traceroute diagnostic started on CPE %s → %s",
            cpe.serial_number,
            host.strip(),
        )
        return ActionResult(
            success=True,
            message=(
                f"Traceroute started on {cpe.serial_number} → {host.strip()}. "
                "Results will appear after the next device inform."
            ),
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Traceroute diagnostic failed for CPE %s: %s", cpe.serial_number, exc
        )
        return ActionResult(success=False, message=f"Failed to start traceroute: {exc}")
