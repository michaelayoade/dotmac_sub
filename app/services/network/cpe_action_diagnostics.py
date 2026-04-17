"""Diagnostic actions for CPE devices."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_cpe_client_or_error,
    persist_data_model_root,
    set_and_verify,
)

logger = logging.getLogger(__name__)


_PING_PATHS = {
    "Device": {
        "host": "IP.Diagnostics.IPPing.Host",
        "count": "IP.Diagnostics.IPPing.NumberOfRepetitions",
        "state": "IP.Diagnostics.IPPing.DiagnosticsState",
    },
    "InternetGatewayDevice": {
        "host": "IPPingDiagnostics.Host",
        "count": "IPPingDiagnostics.NumberOfRepetitions",
        "state": "IPPingDiagnostics.DiagnosticsState",
    },
}

_TRACEROUTE_PATHS = {
    "Device": {
        "host": "IP.Diagnostics.TraceRoute.Host",
        "state": "IP.Diagnostics.TraceRoute.DiagnosticsState",
    },
    "InternetGatewayDevice": {
        "host": "TraceRouteDiagnostics.Host",
        "state": "TraceRouteDiagnostics.DiagnosticsState",
    },
}


def run_ping_diagnostic(
    db: Session, cpe_id: str, host: str, count: int = 4
) -> ActionResult:
    """Run a ping diagnostic from the CPE device via TR-069."""
    if not host or not host.strip():
        return ActionResult(success=False, message="Ping target host is required.")

    resolved, error = get_cpe_client_or_error(db, cpe_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="CPE device resolution failed.")
    cpe, client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)
    persist_data_model_root(cpe, root)
    count = max(1, min(count, 20))
    paths = _PING_PATHS[root]
    params = build_tr069_params(
        root,
        {
            paths["host"]: host.strip(),
            paths["count"]: str(count),
            paths["state"]: "Requested",
        },
    )
    expected = {
        f"{root}.{paths['host']}": host.strip(),
        f"{root}.{paths['count']}": str(count),
    }
    try:
        result = set_and_verify(client, device_id, params, expected=expected)
        logger.info(
            "Ping diagnostic started on CPE %s -> %s (%d pings)",
            cpe.serial_number,
            host.strip(),
            count,
        )
        return ActionResult(
            success=True,
            message=(
                f"Ping diagnostic started on {cpe.serial_number} -> {host.strip()} "
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

    resolved, error = get_cpe_client_or_error(db, cpe_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="CPE device resolution failed.")
    cpe, client, device_id = resolved
    root = detect_data_model_root(db, cpe, client, device_id)
    persist_data_model_root(cpe, root)
    paths = _TRACEROUTE_PATHS[root]
    params = build_tr069_params(
        root,
        {
            paths["host"]: host.strip(),
            paths["state"]: "Requested",
        },
    )
    expected = {f"{root}.{paths['host']}": host.strip()}
    try:
        result = set_and_verify(client, device_id, params, expected=expected)
        logger.info(
            "Traceroute diagnostic started on CPE %s -> %s",
            cpe.serial_number,
            host.strip(),
        )
        return ActionResult(
            success=True,
            message=(
                f"Traceroute started on {cpe.serial_number} -> {host.strip()}. "
                "Results will appear after the next device inform."
            ),
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Traceroute diagnostic failed for CPE %s: %s", cpe.serial_number, exc
        )
        return ActionResult(success=False, message=f"Failed to start traceroute: {exc}")
