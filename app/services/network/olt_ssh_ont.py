"""Focused OLT SSH actions for ONT-level operations."""

from __future__ import annotations

import logging

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


def configure_ont_iphost(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    vlan_id: int,
    ip_mode: str = "dhcp",
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP (IPHOST) via OLT SSH."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        if ip_mode == "dhcp":
            cmd = f"ont ipconfig {port_num} {ont_id} ip-index 0 dhcp vlan {vlan_id}"
        else:
            if not ip_address or not subnet or not gateway:
                return False, "Static IP mode requires ip_address, subnet, and gateway"
            cmd = (
                f"ont ipconfig {port_num} {ont_id} "
                f"ip-index 0 static ip-address {ip_address} "
                f"mask {subnet} gateway {gateway} vlan {vlan_id}"
            )

        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning("IPHOST config failed for ONT %d on OLT %s: %s", ont_id, olt.name, output.strip()[-150:])
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Configured IPHOST for ONT %d on OLT %s (%s VLAN %d)", ont_id, olt.name, ip_mode, vlan_id)
        return True, f"Management IP configured ({ip_mode} on VLAN {vlan_id})"
    except Exception as exc:
        logger.error("Error configuring IPHOST on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


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
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", {}
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", {}

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        cmd = f"display ont ipconfig {parts[0]}/{parts[1]} {port_num} {ont_id}"
        output = core._run_huawei_cmd(channel, cmd)
        config: dict[str, str] = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                config[key.strip()] = value.strip()
        return True, "IPHOST config retrieved", config
    except Exception as exc:
        logger.error("Error getting IPHOST config from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", {}
    finally:
        transport.close()


def reboot_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Reboot an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        channel.send(f"ont reset {port_num} {ont_id}\n")
        output = core._read_until_prompt(channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10)
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning("ONT reset failed for %d on OLT %s: %s", ont_id, olt.name, output.strip()[-150:])
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("ONT %d reset via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} reboot command sent via OMCI"
    except Exception as exc:
        logger.error("Error resetting ONT on OLT %s: %s", olt.name, exc)
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
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        cmd = f"ont internet-config {port_num} {ont_id} ip-index {ip_index}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "internet-config failed for ONT %d on OLT %s: %s",
                ont_id, olt.name, output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Configured internet-config for ONT %d on OLT %s (ip-index %d)", ont_id, olt.name, ip_index)
        return True, f"Internet config activated (ip-index {ip_index})"
    except Exception as exc:
        logger.error("Error configuring internet-config on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


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
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        cmd = f"ont wan-config {port_num} {ont_id} ip-index {ip_index} profile-id {profile_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "wan-config failed for ONT %d on OLT %s: %s",
                ont_id, olt.name, output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Configured wan-config for ONT %d on OLT %s (ip-index %d, profile-id %d)",
            ont_id, olt.name, ip_index, profile_id,
        )
        return True, f"WAN route+NAT mode set (ip-index {ip_index}, profile-id {profile_id})"
    except Exception as exc:
        logger.error("Error configuring wan-config on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


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
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

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
                ont_id, olt.name, output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Configured PPPoE via OMCI for ONT %d on OLT %s (VLAN %d, user %s)",
            ont_id, olt.name, vlan_id, username,
        )
        return True, f"PPPoE configured via OMCI (VLAN {vlan_id}, user {username})"
    except Exception as exc:
        logger.error("Error configuring PPPoE OMCI on OLT %s: %s", olt.name, exc)
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
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

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
                ont_id, eth_port, olt.name, output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Set native VLAN %d on ONT %d eth %d on OLT %s",
            vlan_id, ont_id, eth_port, olt.name,
        )
        return True, f"Native VLAN {vlan_id} set on eth port {eth_port}"
    except Exception as exc:
        logger.error("Error setting port native-vlan on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def factory_reset_ont_omci(olt: OLTDevice, fsp: str, ont_id: int) -> tuple[bool, str]:
    """Full factory reset of an ONT via OMCI from the OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        channel.send(f"ont factory-setting-restore {port_num} {ont_id}\n")
        output = core._read_until_prompt(channel, rf"{config_prompt}|y/n|Y/N", timeout_sec=10)
        if "y/n" in output.lower():
            channel.send("y\n")
            output += core._read_until_prompt(channel, config_prompt, timeout_sec=10)

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Factory reset failed for ONT %d on OLT %s: %s",
                ont_id, olt.name, output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Factory reset ONT %d via OMCI on OLT %s", ont_id, olt.name)
        return True, f"ONT {ont_id} factory reset command sent via OMCI"
    except Exception as exc:
        logger.error("Error factory-resetting ONT on OLT %s: %s", olt.name, exc)
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

    parts = fsp.split("/")
    frame_slot = f"{parts[0]}/{parts[1]}"
    port_num = parts[2]

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        cmd = f"ont remote-ping {port_num} {ont_id} ip-address {ip_address}"
        channel.send(f"{cmd}\n")
        output = core._read_until_prompt(channel, config_prompt, timeout_sec=30)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Remote ping failed for ONT %d on OLT %s: %s",
                ont_id, olt.name, output.strip()[-200:],
            )
            return False, f"Ping failed: {output.strip()[-200:]}"

        logger.info("Remote ping from ONT %d on OLT %s to %s", ont_id, olt.name, ip_address)
        return True, f"Remote ping to {ip_address}: {output.strip()[-200:]}"
    except Exception as exc:
        logger.error("Error running remote ping on OLT %s: %s", olt.name, exc)
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

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(channel, f"interface gpon {frame_slot}", prompt=config_prompt)

        cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        if core.is_error_output(output):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning("TR-069 profile bind failed for ONT %d on OLT %s: %s", ont_id, olt.name, output.strip()[-150:])
            return False, f"OLT rejected: {output.strip()[-150:]}"

        reset_out = core._run_huawei_cmd(channel, f"ont reset {port_num} {ont_id}", prompt=r"[#)]\s*$|y/n")
        if "y/n" in reset_out:
            channel.send("y\n")
            reset_out += core._read_until_prompt(channel, config_prompt, timeout_sec=8)

        if core.is_error_output(reset_out):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            logger.warning(
                "TR-069 profile bound but ONT reset failed for ONT %d on OLT %s: %s",
                ont_id,
                olt.name,
                reset_out.strip()[-150:],
            )
            return False, f"TR-069 profile bound but reset failed: {reset_out.strip()[-150:]}"

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        logger.info("Bound TR-069 profile %d to ONT %d on OLT %s (reset triggered)", profile_id, ont_id, olt.name)
        return True, f"TR-069 profile {profile_id} bound to ONT {ont_id} (reset triggered)"
    except Exception as exc:
        logger.error("Error binding TR-069 profile on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()
