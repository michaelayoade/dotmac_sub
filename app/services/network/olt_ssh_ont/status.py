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


def get_ont_status(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, OntStatusEntry | None]:
    """Query the status of a specific ONT on an OLT port via SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    parts = fsp.split("/")

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        cmd = f"display ont info {parts[0]} {parts[1]} {parts[2]} {ont_id}"
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
