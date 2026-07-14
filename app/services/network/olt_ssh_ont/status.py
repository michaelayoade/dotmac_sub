"""ONT status query functions via OLT SSH."""

from __future__ import annotations

import logging
import re

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    OntStatusEntry,
    RegisteredOntEntry,
)

logger = logging.getLogger(__name__)


def parse_ont_info_detail(output: str) -> dict[str, str | int | None]:
    """Extract richer fields from a ``display ont info <fsp> <id>`` block.

    Returns a dict with keys: ``description``, ``line_profile_id``,
    ``service_profile_id``, ``tr069_profile_id``, ``mgmt_ip``, ``mgmt_vlan``,
    ``distance_m``.
    Missing values are ``None``. Designed to be fed plain-text output —
    callers that want to use it from already-collected SSH output can call
    this directly without re-running the command.

    Multi-line ``Description`` values (Huawei wraps long descs) are joined
    with stripped whitespace on continuations.
    """
    result: dict[str, str | int | None] = {
        "description": None,
        "line_profile_id": None,
        "service_profile_id": None,
        "tr069_profile_id": None,
        "mgmt_ip": None,
        "mgmt_vlan": None,
        "distance_m": None,
    }

    lines = output.splitlines()
    desc_parts: list[str] = []
    in_description = False
    for line in lines:
        # A continuation line is indented and has no field delimiter. Huawei
        # pads long labels beyond column 26, so a fixed-column check can absorb
        # legitimate fields such as ``TR069 server profile ID`` into the
        # description.
        if in_description and line.startswith(" ") and ":" not in line:
            desc_parts.append(line.strip())
            continue
        in_description = False

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key_norm = key.strip().lower()
        value = value.strip()

        if key_norm == "description":
            desc_parts = [value]
            in_description = True
        elif key_norm == "line profile id":
            try:
                result["line_profile_id"] = int(value)
            except ValueError:
                pass
        elif key_norm == "service profile id":
            try:
                result["service_profile_id"] = int(value)
            except ValueError:
                pass
        elif key_norm in {
            "tr069 server profile",
            "tr069 server profile id",
            "tr-069 server profile",
            "tr-069 server profile id",
        }:
            try:
                result["tr069_profile_id"] = int(value)
            except ValueError:
                pass
        elif key_norm == "ont ip 0 address/mask":
            # Format: "172.16.210.20/24"
            ip = value.split("/")[0].strip()
            if ip and ip != "-":
                result["mgmt_ip"] = ip
        elif key_norm == "ont manage vlan":
            try:
                result["mgmt_vlan"] = int(value)
            except ValueError:
                pass
        elif key_norm == "ont distance(m)":
            try:
                result["distance_m"] = int(value)
            except ValueError:
                pass

    if desc_parts:
        joined = "".join(desc_parts)
        result["description"] = joined.strip() or None

    return result


def get_ont_info_detail(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, dict[str, str | int | None] | None]:
    """Like ``get_ont_status`` but returns the richer field set parsed from
    the same ``display ont info`` output.

    Used by the reconciler's OLT reader to populate ``OltObservedFields``
    description / profile ids / mgmt ip / mgmt vlan / distance.
    """
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        cmd = get_huawei_command_profile(olt).display_ont_info(fsp, ont_id)
        output = core._run_huawei_cmd(channel, cmd)

        if core.is_error_output(output):
            return False, f"OLT error: {output.strip()[-200:]}", None

        return True, "ONT info retrieved", parse_ont_info_detail(output)
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting detailed ONT info from OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def get_ont_status(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, OntStatusEntry | None]:
    """Query the status of a specific ONT on an OLT port via SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        from app.services.network.huawei_command_profiles import (
            get_huawei_command_profile,
        )

        cmd = get_huawei_command_profile(olt).display_ont_info(fsp, ont_id)
        output = core._run_huawei_cmd(channel, cmd)

        if core.is_error_output(output):
            return False, f"OLT error: {output.strip()[-200:]}", None

        kv: dict[str, str] = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                kv[key.strip().lower()] = value.strip()

        serial = kv.get("serial number", kv.get("sn", ""))
        entry = OntStatusEntry(
            serial_number=serial,
            run_state=kv.get("run state", "unknown"),
            config_state=kv.get("config state", "unknown"),
            match_state=kv.get("match state", "unknown"),
        )
        return True, "ONT status retrieved", entry
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting ONT status from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def get_registered_ont_serials(
    olt: OLTDevice,
) -> tuple[bool, str, list[RegisteredOntEntry]]:
    """Query all registered ONT serials across all ports on an OLT via SSH."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = core._run_huawei_cmd(
            channel, "display ont info summary all", prompt=r"#\s*$"
        )

        entries: list[RegisteredOntEntry] = []
        # Parse table rows: F/S/P  ONT-ID  SN  ...  RunState
        for line in output.splitlines():
            m = re.match(
                r"\s*(\d+/\s*\d+/\s*\d+)\s+(\d+)\s+(\S+).*?(online|offline|unknown)",
                line,
                re.IGNORECASE,
            )
            if m:
                fsp = m.group(1).replace(" ", "")
                entries.append(
                    RegisteredOntEntry(
                        fsp=fsp,
                        onu_id=int(m.group(2)),
                        real_serial=m.group(3),
                        run_state=m.group(4).lower(),
                    )
                )
        return True, f"Found {len(entries)} registered ONTs", entries
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting registered ONT serials from OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def find_ont_by_serial(
    olt: OLTDevice,
    serial_number: str,
) -> tuple[bool, str, RegisteredOntEntry | None]:
    """Find where an ONT serial is already registered on an OLT.

    Uses 'display ont info by-sn' for direct lookup which is more reliable
    than parsing all registered ONTs.

    Returns:
        (success, message, entry) where entry contains fsp, onu_id, run_state
        if the serial is found, or None if not registered.
    """
    from app.services.network import olt_ssh as core

    # Normalize serial (remove dashes, uppercase)
    normalized_serial = serial_number.replace("-", "").strip().upper()

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        # Use direct serial lookup - much more reliable than parsing all ONTs
        output = core._run_huawei_cmd(
            channel,
            f"display ont info by-sn {normalized_serial}",
            prompt=r"#\s*$",
        )

        # Check for "not exist" or similar error
        if "not exist" in output.lower() or "failure" in output.lower():
            logger.info(
                "ONT serial %s not found on OLT %s",
                serial_number,
                olt.name,
            )
            return True, f"ONT {serial_number} is not registered on {olt.name}", None

        # Parse the output for F/S/P, ONT-ID, and Run state
        fsp_match = re.search(r"F/S/P\s*:\s*(\d+/\d+/\d+)", output)
        ont_id_match = re.search(r"ONT-ID\s*:\s*(\d+)", output)
        run_state_match = re.search(r"Run state\s*:\s*(\w+)", output, re.IGNORECASE)

        if fsp_match and ont_id_match:
            fsp = fsp_match.group(1)
            ont_id = int(ont_id_match.group(1))
            run_state = (
                run_state_match.group(1).lower() if run_state_match else "unknown"
            )

            logger.info(
                "Found existing ONT registration: serial=%s on %s port %s ont_id=%d state=%s",
                serial_number,
                olt.name,
                fsp,
                ont_id,
                run_state,
            )
            return (
                True,
                f"ONT {serial_number} is registered on {fsp} as ONT-ID {ont_id} ({run_state})",
                RegisteredOntEntry(
                    fsp=fsp,
                    onu_id=ont_id,
                    real_serial=normalized_serial,
                    run_state=run_state,
                ),
            )

        # If we got output but couldn't parse it, log for debugging
        logger.warning(
            "Could not parse ONT info output for serial %s on OLT %s: %s",
            serial_number,
            olt.name,
            output[:500],
        )
        return True, f"ONT {serial_number} is not registered on {olt.name}", None

    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error finding ONT by serial %s on OLT %s: %s",
            serial_number,
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()
