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
from typing import Protocol

from app.services.network.reconcile.actions import AcsArmDiagnostic

logger = logging.getLogger(__name__)

#: TR-143 sets DiagnosticsState to this to request a run.
STATE_REQUESTED = "Requested"
#: The device reports this once the transfer finished successfully.
STATE_COMPLETE = "Completed"
#: Anything else the device may report; treated as a failed run, not a retry
#: loop — a CPE that cannot run the test will not start being able to.
TERMINAL_ERROR_PREFIX = "Error_"

#: "Not measured" timestamps. 0001-01-01 is the spec's sentinel; 1900-01-00 is
#: what the production HG8546M fleet actually emits (an invalid date).
_UNSET_TIME_PREFIXES = ("0001-01-01", "1900-01-00", "0000-00-00")


class AcsReader(Protocol):
    """The read slice of the ACS client this module needs.

    These signatures mirror ``GenieACSClient`` exactly. They previously did not:
    an invented ``timeout_sec`` keyword type-checked and passed against a
    hand-written fake, then raised TypeError on the first real call. A
    conformance test now pins this protocol to the concrete client.

    Reads only. Every ACS *write* goes through reconcile/applier.py — the
    ownership contract in tests/architecture/test_huawei_control_plane_writes.py
    exists so device mutations converge on one audited path, and a diagnostic
    that wrote to the NBI directly would be a second, unaudited one.
    """

    def refresh_object(
        self,
        device_id: str,
        object_path: str,
        allow_broad_refresh: bool = False,
        allow_when_pending: bool = False,
    ) -> dict: ...

    def get_parameter_values(
        self,
        device_id: str,
        parameters: list[str],
        allow_when_pending: bool = False,
    ) -> dict: ...


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
    """TR-143 timestamps are ISO 8601 with microseconds, or an unset sentinel.

    The spec's "unknown time" sentinel is 0001-01-01T00:00:00Z, but firmware
    invents its own. The production HG8546M fleet reports
    ``1900-01-00T00:00:00.000000`` - note day ``00``, which is not a valid date
    at all. Both mean "not measured" and neither is an error worth logging, so
    known sentinels are recognised before parsing and any pre-1970 result is
    treated as unset.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith(_UNSET_TIME_PREFIXES):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable TR-143 timestamp %r", raw)
        return None
    # A device that reports a pre-epoch instant has not measured the boundary.
    return None if parsed.year < 1970 else parsed


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
    interface: str | None,
) -> AcsArmDiagnostic:
    """The applier action that requests a TR-143 run.

    ``interface`` is the WAN connection the CPE must run the test over, e.g.
    ``InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1``.
    It has no default on purpose: leaving it unset on a production HG8546M made
    the device fall back to its default route and fail with
    ``Error_InitConnectionFailed``, so the caller must choose the path
    deliberately. Pass ``None`` only to accept the device's own default.

    Which interface is chosen decides *what is measured* - the management WAN
    measures the management path, the PPPoE WAN measures the customer's
    service. They are not interchangeable.

    Writing is not this module's job. It composes the parameter set and hands
    back an action for ``reconcile.applier.apply_plan`` to execute, so the
    diagnostic is audited alongside every other device write.
    """
    paths = _paths(tree, direction)
    if not paths:
        raise ValueError(f"No TR-143 {direction} paths in this parameter tree")

    params: dict[str, object] = {paths["url"]: target_url}
    if interface is not None:
        if "interface" not in paths:
            raise ValueError(
                f"This parameter tree has no TR-143 {direction} interface path"
            )
        params[paths["interface"]] = interface
    # State last: the device starts the run the moment it is set.
    params[paths["state"]] = STATE_REQUESTED

    return AcsArmDiagnostic(
        device_id=device_id,
        params=params,
        label=f"tr143.{direction}",
    )


def prepare_diagnostic_object(
    client: AcsReader,
    device_id: str,
    tree: dict[str, str],
    *,
    direction: str = "download",
) -> str:
    """Enumerate the diagnostics object's children. Returns its path.

    Not optional, and it must precede the arm action: the production ACS holds
    these objects as ``{"_object": true, "_writable": true}`` with no children,
    so a setParameterValues issued first has nothing to write to. This is a
    read/refresh, not a mutation.

    The client owns its own timeout; this call deliberately passes none.
    """
    paths = _paths(tree, direction)
    if not paths:
        raise ValueError(f"No TR-143 {direction} paths in this parameter tree")

    object_path = paths["state"].rsplit(".", 1)[0]
    client.refresh_object(device_id, object_path)
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
