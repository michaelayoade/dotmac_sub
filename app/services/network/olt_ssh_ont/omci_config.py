"""ONT OMCI-based configuration functions (internet, WAN, PPPoE, port) via OLT SSH."""

from __future__ import annotations

import logging

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_ssh_ont._common import _SSH_CONNECTION_ERRORS, _send_slow
from app.services.network.olt_ssh_ont.iphost import _run_ont_config_command

logger = logging.getLogger(__name__)


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

        # Use slow send for interface and command to avoid MA5608T terminal corruption
        _send_slow(channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        cmd = f"ont internet-config {port_num} {ont_id} ip-index {ip_index}"
        _send_slow(channel, cmd)
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=12)

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

        # Use slow send for interface and command to avoid MA5608T terminal corruption
        _send_slow(channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        cmd = f"ont wan-config {port_num} {ont_id} ip-index {ip_index} profile-id {profile_id}"
        _send_slow(channel, cmd)
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=12)

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
    priority: int = 0,  # Kept for backward compatibility, not used in command
    username: str,
    password: str,
) -> tuple[bool, str]:
    """Configure PPPoE WAN via OMCI (OLT-side, not TR-069).

    Uses the Huawei OLT command:
        ont ipconfig <port> <ont-id> ip-index <idx> pppoe vlan <vlan>
            user-account username <user> password <pass>

    This creates a PPPoE WAN connection on the ONT with the specified VLAN
    and credentials. The ip-index should typically be 1 to avoid conflicting
    with management IP on index 0.
    """
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

        # Use slow send for interface and command to avoid MA5608T terminal corruption
        _send_slow(channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        # Huawei MA5608T/MA5800 syntax for PPPoE via OMCI
        cmd = (
            f"ont ipconfig {port_num} {ont_id} ip-index {ip_index} "
            f"pppoe vlan {vlan_id} user-account username {username} password {password}"
        )
        _send_slow(channel, cmd)
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=12)

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

        # Use slow send for interface and command to avoid MA5608T terminal corruption
        _send_slow(channel, f"interface gpon {frame_slot}")
        core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        cmd = (
            f"ont port native-vlan {port_num} {ont_id} eth {eth_port} "
            f"vlan {vlan_id} priority {priority}"
        )
        _send_slow(channel, cmd)
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=12)

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
