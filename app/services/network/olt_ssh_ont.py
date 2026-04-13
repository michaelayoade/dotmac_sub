"""Focused OLT SSH actions for ONT-level operations."""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_validators import (
    ValidationError,
    validate_ip_address,
    validate_ont_id,
    validate_subnet_mask,
    validate_vlan_id,
)

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (
    SSHException,
    OSError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
)


@dataclass
class OntStatusEntry:
    """Status of a single registered ONT on an OLT port."""

    serial_number: str
    run_state: str
    config_state: str
    match_state: str


@dataclass
class RegisteredOntEntry:
    """An ONT serial registered on an OLT."""

    fsp: str
    onu_id: int
    real_serial: str
    run_state: str


def get_ont_status(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, OntStatusEntry | None]:
    """Query the status of a specific ONT on an OLT port via SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    parts = fsp.split("/")
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        cmd = f"display ont info {parts[0]}/{parts[1]} {port_num} {ont_id}"
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


def _run_ont_config_command(
    olt: OLTDevice,
    fsp: str,
    command: str,
    *,
    success_message: str,
) -> tuple[bool, str]:
    """Run a single ONT-scoped config command on a GPON interface."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )
        output = core._run_huawei_cmd(channel, command, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "ONT config command failed on OLT %s: %s",
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"
        return True, success_message
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running ONT config command on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def configure_ont_iphost(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    vlan_id: int,
    ip_mode: str = "dhcp",
    priority: int | None = None,
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP (IPHOST) via OLT SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate numeric parameters before CLI interpolation
    try:
        validate_ont_id(ont_id)
        validate_vlan_id(vlan_id)
    except ValidationError as e:
        return False, e.message

    # Validate IP addresses for static mode before CLI interpolation
    if ip_mode != "dhcp":
        if not ip_address or not subnet or not gateway:
            return False, "Static IP mode requires ip_address, subnet, and gateway"
        try:
            ip_address = validate_ip_address(ip_address, "ip_address")
            subnet = validate_subnet_mask(subnet, "subnet_mask")
            gateway = validate_ip_address(gateway, "gateway")
        except ValidationError as e:
            return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        priority_clause = f" priority {priority}" if priority is not None else ""
        if ip_mode == "dhcp":
            cmd = (
                f"ont ipconfig {port_num} {ont_id} ip-index 0 "
                f"dhcp vlan {vlan_id}{priority_clause}"
            )
        else:
            # ip_address, subnet, gateway already validated above
            cmd = (
                f"ont ipconfig {port_num} {ont_id} "
                f"ip-index 0 static ip-address {ip_address} "
                f"mask {subnet} gateway {gateway} vlan {vlan_id}{priority_clause}"
            )

        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if "make configuration repeatedly" in output.lower():
            logger.info(
                "IPHOST config already present for ONT %d on OLT %s (%s VLAN %d)",
                ont_id,
                olt.name,
                ip_mode,
                vlan_id,
            )
            return (
                True,
                f"Management IP already configured ({ip_mode} on VLAN {vlan_id})",
            )

        if core.is_error_output(output):
            logger.warning(
                "IPHOST config failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        verify_output = core._run_huawei_cmd(
            channel, f"display ont ipconfig {port_num} {ont_id}", prompt=config_prompt
        )
        if core.is_error_output(verify_output):
            logger.warning(
                "IPHOST verification failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                verify_output.strip()[-150:],
            )
            return False, f"OLT verification failed: {verify_output.strip()[-150:]}"

        verified = parse_iphost_config_output(verify_output)
        verified_mode = (verified.get("mode") or "").lower()
        verified_vlan = verified.get("vlan")
        verified_priority = verified.get("priority")
        if ip_mode == "dhcp":
            mode_ok = "dhcp" in verified_mode
            ip_ok = True
        else:
            mode_ok = "static" in verified_mode
            ip_ok = verified.get("ip_address") == ip_address
        vlan_ok = verified_vlan == str(vlan_id)
        priority_ok = priority is None or verified_priority == str(priority)
        if not (mode_ok and ip_ok and vlan_ok and priority_ok):
            logger.warning(
                "IPHOST verification mismatch for ONT %d on OLT %s: expected mode=%s "
                "ip=%s vlan=%s priority=%s, got %s",
                ont_id,
                olt.name,
                ip_mode,
                ip_address,
                vlan_id,
                priority,
                verified,
            )
            return False, "OLT did not apply the requested management IP configuration"

        logger.info(
            "Configured IPHOST for ONT %d on OLT %s (%s VLAN %d)",
            ont_id,
            olt.name,
            ip_mode,
            vlan_id,
        )
        return True, f"Management IP configured ({ip_mode} on VLAN {vlan_id})"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error configuring IPHOST on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def parse_iphost_config_output(output: str) -> dict[str, str]:
    """Parse Huawei ``display ont ipconfig`` output into normalized fields."""
    config: dict[str, str] = {}
    aliases = {
        "ont ip host index": "ip_index",
        "ont iphost index": "ip_index",
        "ont config type": "mode",
        "ont ip": "ip_address",
        "ont subnet mask": "subnet_mask",
        "ont gateway": "gateway",
        "ont primary dns": "primary_dns",
        "ont slave dns": "secondary_dns",
        "ont mac": "mac_address",
        "ont manage vlan": "vlan",
        "ont manage priority": "priority",
        "dscp mapping table index": "dscp_mapping_table_index",
    }
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        raw_key = " ".join(key.strip().lower().split())
        raw_value = value.strip()
        if not raw_key:
            continue
        config[key.strip()] = raw_value
        normalized_key = aliases.get(raw_key)
        if normalized_key:
            config[normalized_key] = raw_value
    return config


def clear_ont_ipconfig(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Best-effort removal of ONT IP configuration for a given IP index."""
    parts = fsp.split("/")
    port_num = parts[2] if len(parts) > 2 else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont ipconfig {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"ONT ipconfig cleared for ip-index {ip_index}",
    )


def get_ont_iphost_config(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, dict[str, str]]:
    """Query current ONT IPHOST configuration from OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, {}

    parts = fsp.split("/")
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", {}

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {parts[0]}/{parts[1]}", prompt=config_prompt
        )

        cmd = f"display ont ipconfig {port_num} {ont_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        config = parse_iphost_config_output(output)
        return True, "IPHOST config retrieved", config
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error getting IPHOST config from OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}", {}
    finally:
        transport.close()


def reboot_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Reboot an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        channel.send(f"ont reset {port_num} {ont_id}\n")
        output = core._read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "ONT reset failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("ONT %d reset via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} reboot command sent via OMCI"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error resetting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def configure_ont_internet_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Activate TCP stack on ONT management WAN via internet-config."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = f"ont internet-config {port_num} {ont_id} ip-index {ip_index}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "internet-config failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Configured internet-config for ONT %d on OLT %s (ip-index %d)",
            ont_id,
            olt.name,
            ip_index,
        )
        return True, f"Internet config activated (ip-index {ip_index})"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error configuring internet-config on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def clear_ont_internet_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Best-effort removal of ONT internet-config state."""
    parts = fsp.split("/")
    port_num = parts[2] if len(parts) > 2 else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont internet-config {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"Internet config cleared for ip-index {ip_index}",
    )


def configure_ont_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
    profile_id: int = 0,
) -> tuple[bool, str]:
    """Set route+NAT mode on ONT management WAN via wan-config."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = f"ont wan-config {port_num} {ont_id} ip-index {ip_index} profile-id {profile_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "wan-config failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Configured wan-config for ONT %d on OLT %s (ip-index %d, profile-id %d)",
            ont_id,
            olt.name,
            ip_index,
            profile_id,
        )
        return (
            True,
            f"WAN route+NAT mode set (ip-index {ip_index}, profile-id {profile_id})",
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error configuring wan-config on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def clear_ont_wan_config(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 0,
) -> tuple[bool, str]:
    """Best-effort removal of ONT wan-config state."""
    parts = fsp.split("/")
    port_num = parts[2] if len(parts) > 2 else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont wan-config {port_num} {ont_id} ip-index {ip_index}",
        success_message=f"WAN config cleared for ip-index {ip_index}",
    )


def configure_ont_pppoe_omci(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    ip_index: int = 1,
    vlan_id: int,
    priority: int = 0,
    username: str,
    password: str,
) -> tuple[bool, str]:
    """Configure PPPoE WAN via OMCI (OLT-side, not TR-069)."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = (
            f"ont ipconfig {port_num} {ont_id} ip-index {ip_index} "
            f"pppoe vlan {vlan_id} priority {priority} "
            f"user {username} password {password}"
        )
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "PPPoE OMCI config failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Configured PPPoE via OMCI for ONT %d on OLT %s (VLAN %d, user %s)",
            ont_id,
            olt.name,
            vlan_id,
            username,
        )
        return True, f"PPPoE configured via OMCI (VLAN {vlan_id}, user {username})"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error configuring PPPoE OMCI on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def configure_ont_port_native_vlan(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    eth_port: int,
    vlan_id: int,
    priority: int = 0,
) -> tuple[bool, str]:
    """Set native VLAN on ONT Ethernet port for bridging mode."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = (
            f"ont port native-vlan {port_num} {ont_id} eth {eth_port} "
            f"vlan {vlan_id} priority {priority}"
        )
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Port native-vlan failed for ONT %d port %d on OLT %s: %s",
                ont_id,
                eth_port,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Set native VLAN %d on ONT %d eth %d on OLT %s",
            vlan_id,
            ont_id,
            eth_port,
            olt.name,
        )
        return True, f"Native VLAN {vlan_id} set on eth port {eth_port}"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error setting port native-vlan on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def factory_reset_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Full factory reset of an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        channel.send(f"ont factory-setting-restore {port_num} {ont_id}\n")
        output = core._read_until_prompt(
            channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10
        )
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Factory reset failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Factory reset ONT %d via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} factory reset command sent via OMCI"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error factory-resetting ONT on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def remote_ping_ont(
    olt: OLTDevice, fsp: str, ont_id: int, ip_address: str
) -> tuple[bool, str]:
    """Initiate a ping from the ONT itself via OMCI remote-ping."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # SECURITY: Validate IP address before CLI interpolation to prevent injection
    try:
        ip_address = validate_ip_address(ip_address, "ip_address")
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = f"ont remote-ping {port_num} {ont_id} ip-address {ip_address}"
        channel.send(f"{cmd}\n")
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=30)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Remote ping failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                output.strip()[-200:],
            )
            return False, f"Ping failed: {output.strip()[-200:]}"

        logger.info(
            "Remote ping from ONT %d on OLT %s to %s", ont_id, olt.name, ip_address
        )
        return True, f"Remote ping to {ip_address}: {output.strip()[-200:]}"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error running remote ping on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def deauthorize_ont(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Delete an ONT from the OLT so it can be rediscovered via autofind."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    # Validate ont_id before CLI interpolation
    try:
        validate_ont_id(ont_id)
    except ValidationError as e:
        return False, e.message

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        delete_out = core._run_huawei_cmd(
            channel,
            f"ont delete {port_num} {ont_id}",
            prompt=r"[#)]\s*$|y/n|Y/N",
        )
        if "y/n" in delete_out.lower():
            channel.send("y\n")
            delete_out += core._read_until_prompt(
                channel, config_prompt, timeout_sec=10
            )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(delete_out):
            logger.warning(
                "ONT delete failed for %d on OLT %s: %s",
                ont_id,
                olt.name,
                delete_out.strip()[-150:],
            )
            return False, f"OLT rejected: {delete_out.strip()[-150:]}"

        logger.info("Deleted ONT %d from OLT %s on %s", ont_id, olt.name, fsp)
        return True, f"ONT {ont_id} deleted from OLT"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error deleting ONT on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def bind_tr069_server_profile(
    olt: OLTDevice, fsp: str, ont_id: int, profile_id: int
) -> tuple[bool, str]:
    """Bind a TR-069 server profile to an ONT via OLT SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]
    logger.info(
        "TR-069 bind requested: olt=%s fsp=%s ont_id=%s profile_id=%s",
        olt.name,
        fsp,
        ont_id,
        profile_id,
    )

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {frame_slot}", prompt=config_prompt
        )

        cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        if core.is_error_output(output) and "unknown command" in output.lower():
            # Some Huawei builds expect only the ONT ID after entering
            # `interface gpon F/S`; the port is already implied by context.
            fallback_cmd = f"ont tr069-server-config {ont_id} profile-id {profile_id}"
            fallback_output = core._run_huawei_cmd(
                channel, fallback_cmd, prompt=config_prompt
            )
            if not core.is_error_output(fallback_output):
                output = fallback_output
        if core.is_error_output(output):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning(
                "TR-069 profile bind failed: olt=%s fsp=%s ont_id=%s profile_id=%s output=%s",
                olt.name,
                fsp,
                ont_id,
                profile_id,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        reset_out = core._run_huawei_cmd(
            channel, f"ont reset {port_num} {ont_id}", prompt=r"[#)]\s*$|y/n"
        )
        if "y/n" in reset_out:
            channel.send("y\n")
            reset_out += core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        if core.is_error_output(reset_out):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            if "ont is not online" in reset_out.lower():
                logger.info(
                    "TR-069 bind succeeded but reset skipped because ONT is offline: olt=%s fsp=%s ont_id=%s profile_id=%s",
                    olt.name,
                    fsp,
                    ont_id,
                    profile_id,
                )
                return (
                    True,
                    f"TR-069 profile {profile_id} bound to ONT {ont_id}; ONT is offline, so reset will occur when it next boots.",
                )
            logger.warning(
                "TR-069 bind succeeded but reset failed: olt=%s fsp=%s ont_id=%s profile_id=%s output=%s",
                olt.name,
                fsp,
                ont_id,
                profile_id,
                reset_out.strip()[-150:],
            )
            return (
                False,
                f"TR-069 profile bound but reset failed: {reset_out.strip()[-150:]}",
            )

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        logger.info(
            "TR-069 bind succeeded and reset triggered: olt=%s fsp=%s ont_id=%s profile_id=%s",
            olt.name,
            fsp,
            ont_id,
            profile_id,
        )
        return (
            True,
            f"TR-069 profile {profile_id} bound to ONT {ont_id} (reset triggered)",
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error binding TR-069 profile on OLT %s: %s", olt.name, exc, exc_info=True
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def unbind_tr069_server_profile(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
) -> tuple[bool, str]:
    """Best-effort removal of an ONT TR-069 server profile binding."""
    parts = fsp.split("/")
    port_num = parts[2] if len(parts) > 2 else "0"
    return _run_ont_config_command(
        olt,
        fsp,
        f"undo ont tr069-server-config {port_num} {ont_id}",
        success_message="TR-069 profile binding cleared",
    )


# Alias for backwards compatibility
delete_ont_registration = deauthorize_ont


# ---------------------------------------------------------------------------
# ONT Authorization Functions
# ---------------------------------------------------------------------------

_FSP_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{1,3}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9\-]+$")


def _validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format is strictly numeric (e.g. '0/2/1')."""
    if not _FSP_RE.match(fsp):
        return False, f"Invalid F/S/P format: {fsp!r} (expected digits/digits/digits)"
    return True, ""


def _validate_serial(serial_number: str) -> tuple[bool, str]:
    """Validate ONT serial number contains only alphanumeric chars and dashes."""
    if not serial_number or not _SERIAL_RE.match(serial_number):
        return False, f"Invalid serial number format: {serial_number!r}"
    return True, ""


def _safe_profile_name(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", str(name or "ACS")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "ACS")[:48]


def _load_linked_acs_payload(olt: OLTDevice) -> dict[str, object] | None:
    """Load the linked TR-069 ACS server config for an OLT."""
    from app.services.credential_crypto import decrypt_credential

    server = None
    try:
        server = getattr(olt, "tr069_acs_server", None)
    except AttributeError:
        server = None

    if server is None and getattr(olt, "tr069_acs_server_id", None):
        try:
            from app.db import SessionLocal
            from app.models.tr069 import Tr069AcsServer

            with SessionLocal() as db:
                server = db.get(Tr069AcsServer, str(olt.tr069_acs_server_id))
                if server is None:
                    return None
                password = (
                    decrypt_credential(server.cwmp_password)
                    if server.cwmp_password
                    else ""
                )
                return {
                    "name": server.name,
                    "acs_url": server.cwmp_url or "",
                    "username": server.cwmp_username or "",
                    "password": password or "",
                    "inform_interval": server.periodic_inform_interval or 300,
                }
        except (ImportError, LookupError, AttributeError) as exc:
            logger.warning("Failed to load linked ACS for OLT %s: %s", olt.name, exc)
            return None

    if server is None or not getattr(server, "cwmp_url", None):
        return None
    password = (
        decrypt_credential(server.cwmp_password)
        if getattr(server, "cwmp_password", None)
        else ""
    )
    return {
        "name": getattr(server, "name", "ACS"),
        "acs_url": server.cwmp_url or "",
        "username": server.cwmp_username or "",
        "password": password or "",
        "inform_interval": getattr(server, "periodic_inform_interval", None) or 300,
    }


def _auto_bind_tr069_after_authorize(
    olt: OLTDevice, fsp: str, ont_id: int | None
) -> None:
    """Best-effort: bind a newly authorized ONT to the OLT's linked ACS profile.

    Called after successful ONT authorization. Skips when the OLT has no linked
    ACS, and creates the OLT profile if the linked ACS profile does not exist.
    """
    from app.services.network.olt_ssh_profiles import (
        create_tr069_server_profile,
        get_tr069_server_profiles,
    )
    from app.services.network.tr069_profile_matching import (
        match_tr069_profile,
        normalize_acs_url,
    )

    if ont_id is None:
        return
    try:
        payload = _load_linked_acs_payload(olt)
        if payload is None or not str(payload.get("acs_url") or "").strip():
            logger.info("Skipping TR-069 auto-bind for OLT %s: no linked ACS", olt.name)
            return

        ok, _msg, profiles = get_tr069_server_profiles(olt)
        if not ok:
            return
        target_url = normalize_acs_url(str(payload["acs_url"]))
        target_username = str(payload.get("username") or "").strip()
        profile = match_tr069_profile(
            profiles,
            acs_url=str(payload["acs_url"]),
            acs_username=target_username,
        )
        profile_id = profile.profile_id if profile else None

        if profile_id is None:
            profile_name = f"ACS {_safe_profile_name(str(payload.get('name') or ''))}"
            ok, msg = create_tr069_server_profile(
                olt,
                profile_name=profile_name,
                acs_url=str(payload["acs_url"]),
                username=target_username,
                password=str(payload.get("password") or ""),
                inform_interval=int(str(payload.get("inform_interval") or 300)),
            )
            if not ok:
                logger.warning(
                    "Auto-create TR-069 profile failed for OLT %s: %s",
                    olt.name,
                    msg,
                )
                return
            ok, _msg, profiles = get_tr069_server_profiles(olt)
            if not ok:
                return
            profile = match_tr069_profile(
                profiles,
                acs_url=str(payload["acs_url"]),
                acs_username=target_username,
            )
            profile_id = profile.profile_id if profile else None
        if profile_id is None:
            logger.warning(
                "Could not resolve TR-069 profile for linked ACS %s on OLT %s",
                target_url,
                olt.name,
            )
            return

        ok, msg = bind_tr069_server_profile(
            olt, fsp=fsp, ont_id=ont_id, profile_id=profile_id
        )
        if ok:
            logger.info(
                "Auto-bound ONT %d on %s to TR-069 profile %d",
                ont_id,
                fsp,
                profile_id,
            )
        else:
            logger.warning(
                "Auto-bind TR-069 failed for ONT %d on %s: %s", ont_id, fsp, msg
            )
    except (*_SSH_CONNECTION_ERRORS, ValueError, RuntimeError) as exc:
        logger.warning("Auto-bind TR-069 error for ONT %d: %s", ont_id, exc)


def authorize_ont(
    olt: OLTDevice,
    fsp: str,
    serial_number: str,
    *,
    line_profile_id: int | None = None,
    service_profile_id: int | None = None,
) -> tuple[bool, str, int | None]:
    """SSH into OLT and register an ONT via sn-auth on the given port.

    Args:
        olt: The OLT device to connect to.
        fsp: Frame/Slot/Port string, e.g. "0/2/1".
        serial_number: ONT serial in vendor format, e.g. "HWTC-7D4733C3".
        line_profile_id: OLT-local line profile ID resolved before authorization.
        service_profile_id: OLT-local service profile ID resolved before authorization.

    Returns:
        Tuple of (success, message, assigned_ont_id).
    """
    from app.services.network import olt_ssh as core

    if line_profile_id is None or service_profile_id is None:
        return (
            False,
            "OLT authorization profiles were not resolved; refusing to use static profile defaults.",
            None,
        )
    line_pid = line_profile_id
    srv_pid = service_profile_id
    ok, err = _validate_fsp(fsp)
    if not ok:
        return False, err, None
    ok, err = _validate_serial(serial_number)
    if not ok:
        return False, err, None

    try:
        transport, channel, policy = core._open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        # Enter enable mode
        channel.send("enable\n")
        core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

        # Enter config mode
        config_prompt = r"[#)]\s*$"
        channel.send("config\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Enter GPON interface for the frame/slot
        parts = fsp.split("/")
        frame_slot = f"{parts[0]}/{parts[1]}"
        port_num = parts[2]

        channel.send(f"interface gpon {frame_slot}\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=5)

        # Authorize the ONT — sn-auth uses the serial without dashes
        sn_clean = serial_number.replace("-", "")
        channel.send(
            f"ont add {port_num} sn-auth {sn_clean} omci ont-lineprofile-id {line_pid} ont-srvprofile-id {srv_pid}\n"
        )
        # Huawei may prompt "{ <cr>|desc<K>|ont-type<K> }:" — send CR to confirm
        initial = core._read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=10)
        if "<cr>" in initial:
            channel.send("\n")
            output = core._read_until_prompt(channel, r"[#)]\s*$", timeout_sec=10)
        else:
            output = initial

        # Exit config mode
        channel.send("quit\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=3)
        channel.send("quit\n")
        core._read_until_prompt(channel, config_prompt, timeout_sec=3)

        # Check for success indicators
        ont_id_match = re.search(r"ont-?id\D+(\d+)", output, flags=re.IGNORECASE)
        ont_id = int(ont_id_match.group(1)) if ont_id_match else None

        if "success" in output.lower() or "ont-id" in output.lower():
            logger.info(
                "Authorized ONT %s on OLT %s port %s",
                serial_number,
                olt.name,
                fsp,
            )
            # Auto-bind to the OLT's linked ACS TR-069 profile if configured.
            _auto_bind_tr069_after_authorize(olt, fsp, ont_id)

            message = f"ONT {serial_number} authorized on port {fsp}"
            if ont_id is not None:
                message += f" (ONT-ID {ont_id})"
            return True, message, ont_id
        if core.is_error_output(output):
            logger.warning(
                "Failed to authorize ONT %s on OLT %s: %s",
                serial_number,
                olt.name,
                output.strip(),
            )
            return False, f"OLT rejected command: {output.strip()[-200:]}", None

        # Ambiguous — return output for inspection
        logger.info(
            "ONT authorize command sent for %s on OLT %s, output: %s",
            serial_number,
            olt.name,
            output.strip(),
        )
        return True, f"Command sent for {serial_number} on port {fsp}", ont_id
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error authorizing ONT %s on OLT %s: %s",
            serial_number,
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()
