"""TR-143 link speed test — measured at the router, not in the browser.

The portal speed test measures the customer's *device over their WiFi*. That is
the wrong instrument for "is Dotmac delivering my plan?": consumer WiFi is the
bottleneck far more often than the access link is, so a browser-only number
makes good links look bad and hands the customer something to argue with. This
module measures the other end — the ONT/router's own throughput to a Dotmac
target — so the two can be compared. The delta between them *is* the diagnosis:
link at plan rate plus poor device result means customer-side WiFi, which
resolves with no engineer and no ticket.

Fleet reality (production ACS survey, 2026-07-23): 363 of 406 devices expose
TR-143, and every one of them does so through the **TR-098** tree
(``InternetGatewayDevice.DownloadDiagnostics``). Zero expose the TR-181 tree.
The objects are ``_writable`` but their child parameters have never been
enumerated by the ACS, so a refresh must precede any set.

TR-143 is genuinely asynchronous. The CPE runs the transfer on its own schedule
and reports completion on a later inform — there is no synchronous result to
wait for inside a request. So this module splits the operation:

    prepare_diagnostic_object()  -> enumerate the object's children (a read)
    build_arm_action()           -> the applier action that requests the run
    harvest_link_speed_test()    -> read the completed result

A caller (task, admin action) drives the sequence and hands the arm action to
reconcile.applier.apply_plan. Nothing here writes to the ACS, and nothing here
blocks on the device.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.services.network.reconcile.actions import AcsArmDiagnostic

logger = logging.getLogger(__name__)

#: TR-143 sets DiagnosticsState to this to request a run.
STATE_REQUESTED = "Requested"
#: The device reports this once the transfer finished successfully.
STATE_COMPLETE = "Completed"
#: Anything else the device may report; treated as a failed run, not a retry
#: loop — a CPE that cannot run the test will not start being able to.
TERMINAL_ERROR_PREFIX = "Error_"

#: A test that never completes must not pin a slot forever.
DEFAULT_ARM_TIMEOUT_SEC = 300


class AcsReader(Protocol):
    """The read slice of the ACS client this module needs.

    Reads only. Every ACS *write* goes through reconcile/applier.py — the
    ownership contract in tests/architecture/test_huawei_control_plane_writes.py
    exists so device mutations converge on one audited path, and a diagnostic
    that wrote to the NBI directly would be a second, unaudited one.
    """

    def refresh_object(self, device_id: str, path: str, *, timeout_sec: int) -> Any: ...

    def get_parameter_values(
        self, device_id: str, paths: list[str]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LinkSpeedResult:
    """One completed direction of a TR-143 run."""

    direction: str  # "download" | "upload"
    mbps: float
    bytes_transferred: int
    started_at: datetime | None
    finished_at: datetime | None
    diagnostics_state: str

    @property
    def succeeded(self) -> bool:
        return self.diagnostics_state == STATE_COMPLETE and self.mbps > 0


def _parse_tr143_time(value: object) -> datetime | None:
    """TR-143 timestamps are ISO 8601 with microseconds, or the unset sentinel.

    The spec's "unknown time" sentinel is 0001-01-01T00:00:00Z; a device that
    reports it has not actually measured the boundary, so it must not be
    treated as a real instant.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable TR-143 timestamp %r", raw)
        return None


def throughput_mbps(
    byte_count: object, bom_time: object, eom_time: object
) -> tuple[float, datetime | None, datetime | None]:
    """Megabits per second between the first and last byte of the transfer.

    TR-143 defines throughput over BOMTime..EOMTime rather than the whole
    request, so connection setup and teardown are excluded. Returns 0.0 when
    the device did not report a usable window — an unmeasurable run is not a
    slow one, and reporting it as 0 Mbps would libel a healthy link.
    """
    started = _parse_tr143_time(bom_time)
    finished = _parse_tr143_time(eom_time)
    if started is None or finished is None:
        return 0.0, started, finished

    seconds = (finished - started).total_seconds()
    if seconds <= 0:
        return 0.0, started, finished

    try:
        total_bytes = int(str(byte_count).strip())
    except (TypeError, ValueError):
        return 0.0, started, finished
    if total_bytes <= 0:
        return 0.0, started, finished

    return round((total_bytes * 8) / seconds / 1_000_000, 3), started, finished


def _paths(tree: dict[str, str], direction: str) -> dict[str, str]:
    prefix = f"diag.{direction}."
    return {
        key.removeprefix(prefix): path
        for key, path in tree.items()
        if key.startswith(prefix)
    }


def build_arm_action(
    device_id: str,
    tree: dict[str, str],
    *,
    direction: str = "download",
    target_url: str,
) -> AcsArmDiagnostic:
    """The applier action that requests a TR-143 run.

    Writing is not this module's job. It composes the parameter set and hands
    back an action for ``reconcile.applier.apply_plan`` to execute, so the
    diagnostic is audited alongside every other device write.
    """
    paths = _paths(tree, direction)
    if not paths:
        raise ValueError(f"No TR-143 {direction} paths in this parameter tree")

    return AcsArmDiagnostic(
        device_id=device_id,
        params={paths["url"]: target_url, paths["state"]: STATE_REQUESTED},
        label=f"tr143.{direction}",
    )


def prepare_diagnostic_object(
    client: AcsReader,
    device_id: str,
    tree: dict[str, str],
    *,
    direction: str = "download",
    timeout_sec: int = DEFAULT_ARM_TIMEOUT_SEC,
) -> str:
    """Enumerate the diagnostics object's children. Returns its path.

    Not optional, and it must precede the arm action: the production ACS holds
    these objects as ``{"_object": true, "_writable": true}`` with no children,
    so a setParameterValues issued first has nothing to write to. This is a
    read/refresh, not a mutation.
    """
    paths = _paths(tree, direction)
    if not paths:
        raise ValueError(f"No TR-143 {direction} paths in this parameter tree")

    object_path = paths["state"].rsplit(".", 1)[0]
    client.refresh_object(device_id, object_path, timeout_sec=timeout_sec)
    logger.info(
        "tr143_object_refreshed",
        extra={
            "event": "tr143_object_refreshed",
            "device_id": device_id,
            "direction": direction,
            "object_path": object_path,
        },
    )
    return object_path


def harvest_link_speed_test(
    client: AcsReader,
    device_id: str,
    tree: dict[str, str],
    *,
    direction: str = "download",
) -> LinkSpeedResult | None:
    """Read a finished run. Returns None while the device is still working.

    A caller polling this must treat None as "not yet" and an ``Error_`` state
    as terminal — retrying a device that reported Error_CannotResolveHostName
    only burns its link again.
    """
    paths = _paths(tree, direction)
    if not paths:
        raise ValueError(f"No TR-143 {direction} paths in this parameter tree")

    byte_key = "test_bytes" if "test_bytes" in paths else "total_bytes"
    wanted = [paths["state"], paths[byte_key], paths["bom_time"], paths["eom_time"]]
    values = client.get_parameter_values(device_id, wanted)

    state = str(values.get(paths["state"]) or "").strip()
    if not state or state == STATE_REQUESTED:
        return None

    mbps, started, finished = throughput_mbps(
        values.get(paths[byte_key]),
        values.get(paths["bom_time"]),
        values.get(paths["eom_time"]),
    )
    if state.startswith(TERMINAL_ERROR_PREFIX):
        logger.info(
            "tr143_failed",
            extra={
                "event": "tr143_failed",
                "device_id": device_id,
                "direction": direction,
                "state": state,
            },
        )

    try:
        transferred = int(str(values.get(paths[byte_key]) or 0).strip())
    except (TypeError, ValueError):
        transferred = 0

    return LinkSpeedResult(
        direction=direction,
        mbps=mbps,
        bytes_transferred=transferred,
        started_at=started,
        finished_at=finished,
        diagnostics_state=state,
    )
