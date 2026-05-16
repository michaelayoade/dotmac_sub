"""ACS-side reader — GenieACS NBI query into ``AcsObservedFields``.

One ``list_devices`` query per ONT with a focused projection. The reader
trusts the GenieACS CWMP cache — staleness is bounded by the device's
PeriodicInformInterval, and post-write ``VERIFICATION_MISMATCH`` catches the
rare stale-cache case. No ``refreshObject`` round-trip on every read.

The device-id format is ``{OUI}-{ProductClass}-{SerialNumber}``. Since we
don't always know OUI/ProductClass from ``OntUnit``, the query uses a
trailing-serial regex match — this is the same pattern ``GenieACSClient.get_device``
falls back to.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from app.services.genieacs_client import GenieACSError

from ..state import AcsObservedFields, OntDesiredState
from ._types import ReadResult

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


# Projection paths read from the device document. Kept here (rather than
# hardcoded in the parser) so adding a field is a single-line change.
_PROJECTION_PATHS: tuple[str, ...] = (
    "_lastInform",
    "_lastBoot",
    "_lastBootstrap",
    # TR-098 (InternetGatewayDevice) — the HG8546M / EG8145V5 fleet.
    "InternetGatewayDevice.DeviceInfo.SoftwareVersion",
    "InternetGatewayDevice.ManagementServer.PeriodicInformInterval",
    "InternetGatewayDevice.ManagementServer.ConnectionRequestUsername",
    "InternetGatewayDevice.ManagementServer.ConnectionRequestPassword",
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice",
    "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
    # TR-181 (Device) — for any future ONTs on that data model.
    "Device.DeviceInfo.SoftwareVersion",
    "Device.ManagementServer.PeriodicInformInterval",
    "Device.ManagementServer.ConnectionRequestUsername",
    "Device.ManagementServer.ConnectionRequestPassword",
)


def read_acs_state(
    client: Any,
    desired: OntDesiredState,
    *,
    deadline: datetime | None = None,
) -> ReadResult[AcsObservedFields]:
    """Read the ACS-observed fields for one ONT.

    Args:
        client: A ``GenieACSClient`` (or any object with a ``list_devices``
            method returning a list of device dicts).
        desired: The ONT's desired state. Used to construct the device-id
            match.
        deadline: Optional cutoff. The underlying HTTP client has its own
            timeout; ``reconcile_ont`` enforces the outer budget.

    Returns:
        ``ReadResult[AcsObservedFields]``. When the device hasn't yet
        bootstrapped to ACS, returns ``success=True`` with an
        ``acs_present=False`` observation — the planner will plan to wait
        for the next Inform after pushing OLT-side mgmt config.
    """
    query = _query_for_serial(desired.serial_number)
    projection = ",".join(_PROJECTION_PATHS)

    try:
        devices = client.list_devices(query=query, projection=projection)
    except GenieACSError as exc:
        # The client raises GenieACSError for any non-2xx; treat as
        # unreachable so the precondition layer fast-fails before writes.
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error=str(exc),
        )
    except Exception as exc:
        # Defensive: log and report unreachable. We don't want a misshaped
        # NBI response to abort an entire reconcile when the cleaner outcome
        # is "OLT side still readable, ACS side blocked, fast-fail".
        logger.warning(
            "acs_reader_unexpected_error",
            extra={"error": str(exc), "serial": desired.serial_number},
        )
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error=str(exc),
        )

    if not devices:
        # No device matched. That's a clean read; the ONT hasn't informed yet.
        return ReadResult(
            success=True,
            unreachable=False,
            observed=_absent_fields(),
            error=None,
        )

    device = devices[0]
    observed = _parse_device(device)

    # Ghost-instance recovery. ACS may have cached ``setParameterValues``
    # writes against a ``WANPPPConnection.<n>`` path that never existed on
    # the device (HG8546M V5R019C10S100 silently no-ops these — no fault is
    # raised). Subsequent reconciles then see Username/Enable/etc. populated
    # and skip the addObject, so PPP never dials. Tell-tale: instance index
    # resolved but ``ConnectionStatus`` has no reported ``_value``. Force a
    # narrow ``refreshObject`` on the affected WCD and re-parse once.
    if _looks_like_ghost_wan_instance(observed) and hasattr(client, "refresh_object"):
        observed = _refresh_and_reparse(client, device, observed, query, projection)

    return ReadResult(
        success=True,
        unreachable=False,
        observed=observed,
        error=None,
    )


def _looks_like_ghost_wan_instance(observed: AcsObservedFields) -> bool:
    """The reader resolved a WAN PPP instance from the cache, but
    ``ConnectionStatus`` has no ``_value`` — the device never reported PPP
    state for that instance. Strongest single signal that the cached
    parameter values landed on a non-existent CWMP path.
    """
    return (
        observed.acs_observed_wan_instance_index is not None
        and observed.acs_observed_wan_connection_status is None
    )


def _refresh_and_reparse(
    client: Any,
    device: dict[str, Any],
    observed: AcsObservedFields,
    query: dict[str, Any],
    projection: str,
) -> AcsObservedFields:
    """One-shot refresh of the WCD subtree, then re-fetch + re-parse. Any
    failure falls through with the original observation — the planner has
    its own safety nets and we don't want to break sweeps on a flaky ACS.
    """
    device_id = str(device.get("_id") or "").strip()
    wcd = observed.acs_observed_wan_wcd_index
    if not device_id or wcd is None:
        return observed
    refresh_path = f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{wcd}"
    try:
        client.refresh_object(
            device_id,
            refresh_path,
            allow_when_pending=True,
        )
        refreshed = client.list_devices(query=query, projection=projection)
    except Exception as exc:
        logger.info(
            "acs_reader_ghost_refresh_failed",
            extra={
                "error": str(exc),
                "device_id": device_id,
                "refresh_path": refresh_path,
            },
        )
        return observed
    if not refreshed:
        return observed
    return _parse_device(refreshed[0])


# ── Query / parse helpers ───────────────────────────────────────────────────


def _query_for_serial(serial_number: str) -> dict[str, Any]:
    """Build the GenieACS query that matches any device whose ``_id`` ends in
    the given serial. Mirrors ``GenieACSClient.get_device``'s fallback path.
    """
    escaped = re.escape(serial_number)
    return {"_id": {"$regex": f".*-{escaped}$"}}


def _parse_device(device: dict[str, Any]) -> AcsObservedFields:
    """Map a GenieACS device document to ``AcsObservedFields``.

    The document is a nested dict where leaves have a ``_value`` key plus
    optional ``_timestamp`` / ``_type``. We try both TR-098 and TR-181 roots
    so the reader works regardless of which data model the device uses.
    """
    igd = device.get("InternetGatewayDevice") or {}
    dev_root = device.get("Device") or {}
    igd_ms = _path(igd, "ManagementServer")
    dev_ms = _path(dev_root, "ManagementServer")
    igd_info = _path(igd, "DeviceInfo")
    dev_info = _path(dev_root, "DeviceInfo")

    wan_root = _path(igd, "WANDevice", "1", "WANConnectionDevice") or {}
    wcd_index, instance_index, wan_ppp = _resolve_wan_ppp(wan_root)

    return AcsObservedFields(
        acs_present=True,
        acs_last_inform_at=_parse_timestamp(device.get("_lastInform")),
        acs_last_boot_at=_parse_timestamp(device.get("_lastBoot")),
        acs_last_bootstrap_at=_parse_timestamp(device.get("_lastBootstrap")),
        acs_observed_software_version=_first_not_none(
            _value(igd_info, "SoftwareVersion"),
            _value(dev_info, "SoftwareVersion"),
        ),
        acs_observed_pppoe_username=_value(wan_ppp, "Username"),
        acs_observed_pppoe_enable=_value_bool(wan_ppp, "Enable"),
        acs_observed_wan_vlan=_value_int(wan_ppp, "X_HW_VLAN"),
        acs_observed_wan_external_ip=_value(wan_ppp, "ExternalIPAddress"),
        acs_observed_wan_connection_status=_value(wan_ppp, "ConnectionStatus"),
        acs_observed_nat_enabled=_value_bool(wan_ppp, "NATEnabled"),
        acs_observed_dhcp_enabled=_value_bool(
            _path(igd, "LANDevice", "1", "LANHostConfigManagement"),
            "DHCPServerEnable",
        ),
        acs_observed_ssid=_value(
            _path(igd, "LANDevice", "1", "WLANConfiguration", "1"),
            "SSID",
        ),
        acs_observed_periodic_inform_interval_sec=_first_not_none(
            _value_int(igd_ms, "PeriodicInformInterval"),
            _value_int(dev_ms, "PeriodicInformInterval"),
        ),
        acs_observed_cr_username_set=_first_not_none(
            _value_present(igd_ms, "ConnectionRequestUsername"),
            _value_present(dev_ms, "ConnectionRequestUsername"),
        ),
        acs_observed_cr_password_set=_first_not_none(
            _value_present(igd_ms, "ConnectionRequestPassword"),
            _value_present(dev_ms, "ConnectionRequestPassword"),
        ),
        acs_observed_wan_wcd_index=wcd_index,
        acs_observed_wan_instance_index=instance_index,
    )


def _absent_fields() -> AcsObservedFields:
    return AcsObservedFields(
        acs_present=False,
        acs_last_inform_at=None,
        acs_last_boot_at=None,
        acs_last_bootstrap_at=None,
        acs_observed_software_version=None,
        acs_observed_pppoe_username=None,
        acs_observed_pppoe_enable=None,
        acs_observed_wan_vlan=None,
        acs_observed_wan_external_ip=None,
        acs_observed_wan_connection_status=None,
        acs_observed_nat_enabled=None,
        acs_observed_dhcp_enabled=None,
        acs_observed_ssid=None,
        acs_observed_periodic_inform_interval_sec=None,
        acs_observed_cr_username_set=None,
        acs_observed_cr_password_set=None,
        acs_observed_wan_wcd_index=None,
        acs_observed_wan_instance_index=None,
    )


def _resolve_wan_ppp(
    wan_connection_device: dict[str, Any],
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """Locate the first WANPPPConnection instance under any WCD slot.

    Returns ``(wcd_index, instance_index, wan_ppp_dict)``. The fleet's
    HG8546M devices typically expose ``WANConnectionDevice.1.WANPPPConnection.1``
    but provisioned ONTs sometimes have it on ``.2`` (e.g. UnitedAbuja). This
    helper scans the live tree for whichever slot actually carries a
    ``WANPPPConnection`` so the rest of the parser doesn't hardcode ``.1``.
    """
    for wcd_key, wcd_val in (wan_connection_device or {}).items():
        if not wcd_key.isdigit() or not isinstance(wcd_val, dict):
            continue
        wan_ppp_root = wcd_val.get("WANPPPConnection")
        if not isinstance(wan_ppp_root, dict):
            continue
        for ppp_key, ppp_val in wan_ppp_root.items():
            if not ppp_key.isdigit() or not isinstance(ppp_val, dict):
                continue
            return int(wcd_key), int(ppp_key), ppp_val
    return None, None, None


def _first_not_none(*values):
    """Return the first non-None value. Used in place of ``a or b`` when
    falsy-but-not-None values (empty strings, False, 0) are meaningful."""
    for value in values:
        if value is not None:
            return value
    return None


def _path(node: dict[str, Any] | None, *keys: str) -> dict[str, Any] | None:
    current: Any = node
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _value(node: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(node, dict):
        return None
    leaf = node.get(key)
    if not isinstance(leaf, dict):
        return None
    raw = leaf.get("_value")
    return str(raw) if raw is not None else None


def _value_present(node: dict[str, Any] | None, key: str) -> bool | None:
    """Distinguish "field not exposed" (None) from "field present but empty" (False)
    and "field has a value" (True). Used for write-only credentials where the
    actual value isn't readable, only presence."""
    if not isinstance(node, dict):
        return None
    leaf = node.get(key)
    if not isinstance(leaf, dict):
        return None
    raw = leaf.get("_value")
    if raw is None:
        return None
    return bool(str(raw))


def _value_bool(node: dict[str, Any] | None, key: str) -> bool | None:
    raw = _value(node, key)
    if raw is None:
        return None
    return str(raw).strip().lower() in {"true", "1", "yes", "on"}


def _value_int(node: dict[str, Any] | None, key: str) -> int | None:
    raw = _value(node, key)
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: Any) -> datetime | None:
    """GenieACS emits ISO-8601 timestamps with trailing ``Z``. Tolerate both."""
    if raw is None:
        return None
    from datetime import datetime as _dt

    text = str(raw)
    try:
        if text.endswith("Z"):
            return _dt.fromisoformat(text[:-1] + "+00:00")
        return _dt.fromisoformat(text)
    except ValueError:
        return None
