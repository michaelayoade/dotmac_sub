"""OLT SSH actions for ONT autofind (unregistered ONT discovery)."""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)


@dataclass(frozen=True)
class AutofindEntry:
    """A single ONT discovered via Huawei autofind."""

    fsp: str  # Frame/Slot/Port e.g. "0/2/1"
    serial_number: str  # e.g. "HWTC-7D4733C3"
    serial_hex: str  # e.g. "485754437D4733C3"
    vendor_id: str
    model: str  # EquipmentID e.g. "EG8145V5"
    software_version: str
    mac: str
    equipment_sn: str
    autofind_time: str


from app.services.network.serial_utils import normalize as _normalize_vendor_serial


def _parse_huawei_autofind(output: str) -> list[AutofindEntry]:
    """Parse the output of ``display ont autofind all``."""
    entries: list[AutofindEntry] = []
    blocks = re.split(r"-{10,}", output)
    for block in blocks:
        current: dict[str, str] = {}
        lines = block.strip().splitlines()
        for line in lines:
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
        sn_raw = current.get("Ont SN", "")
        if not sn_raw:
            continue
        # Extract "HWTC-7D4733C3" from "485754437D4733C3 (HWTC-7D4733C3)"
        sn_match = re.search(r"\(([^)]+)\)", sn_raw)
        serial_display = _normalize_vendor_serial(
            sn_match.group(1) if sn_match else sn_raw.split()[0]
        )
        serial_hex = sn_raw.split()[0] if " " in sn_raw else sn_raw
        entries.append(
            AutofindEntry(
                fsp=current.get("F/S/P", ""),
                serial_number=serial_display,
                serial_hex=serial_hex,
                vendor_id=current.get("VendorID", ""),
                model=current.get("Ont EquipmentID", ""),
                software_version=current.get("Ont SoftwareVersion", ""),
                mac=current.get("Ont MAC", ""),
                equipment_sn=current.get("Ont Equipment SN", ""),
                autofind_time=current.get("Ont autofind time", ""),
            )
        )
    return entries


def get_autofind_onts(olt: OLTDevice) -> tuple[bool, str, list[AutofindEntry]]:
    """SSH into OLT and retrieve unregistered ONTs from autofind table.

    Returns:
        Tuple of (success, message, list of autofind entries).
    """
    from app.services.network.olt_ssh import _open_shell, _read_until_prompt

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        # Enter enable mode
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        channel.send("display ont autofind all\n")
        # Huawei prompts "{ <cr>||<K> }:" — send CR to confirm
        initial = _read_until_prompt(channel, r"#\s*$|<cr>", timeout_sec=10)
        if "<cr>" in initial:
            channel.send("\n")
            output = _read_until_prompt(channel, r"#\s*$", timeout_sec=15)
        else:
            output = initial
        entries = _parse_huawei_autofind(output)
        count = len(entries)
        msg = f"Found {count} unregistered ONT{'s' if count != 1 else ''}"
        return True, msg, entries
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error reading autofind from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error reading autofind: {exc}", []
    finally:
        transport.close()
