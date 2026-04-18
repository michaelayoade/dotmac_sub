"""OLT SSH actions for profile management (line, service, TR-069).

This module contains all profile-related dataclasses, parsers, and SSH functions
for querying and creating profiles on Huawei OLTs.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass, field

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)

# Specific SSH-related exceptions that can occur during OLT operations
_SSH_CONNECTION_ERRORS = (SSHException, OSError, socket.timeout, TimeoutError, ConnectionError)


@dataclass
class OltProfileEntry:
    """A single OLT profile entry (line, service, TR-069, or WAN)."""

    profile_id: int
    name: str
    type: str = ""
    binding_count: int = 0
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class Tr069ServerProfile:
    """A TR-069 server profile with detail fields."""

    profile_id: int
    name: str
    acs_url: str = ""
    acs_username: str = ""
    inform_interval: int = 0
    binding_count: int = 0


_TR069_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,64}$")


def _parse_profile_table_legacy(
    output: str, id_col: int = 0, name_col: int = 1
) -> list[OltProfileEntry]:
    """Legacy regex parser for Huawei profile display output.

    Used as fallback when TextFSM parsing fails.
    """
    entries: list[OltProfileEntry] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("="):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[id_col])
        except (ValueError, IndexError):
            continue
        name = parts[name_col] if len(parts) > name_col else ""
        entries.append(OltProfileEntry(profile_id=pid, name=name))
    return entries


def _parse_profile_table(
    output: str, id_col: int = 0, name_col: int = 1
) -> list[OltProfileEntry]:
    """Parse Huawei profile display output into structured entries.

    Uses TextFSM template for robust parsing with fallback to legacy regex.
    """
    # Import TextFSM parser if available
    try:
        from app.services.network.parsers import (
            parse_profile_table as _textfsm_parse_profile_table,
        )

        _TEXTFSM_AVAILABLE = True
    except ImportError:
        _TEXTFSM_AVAILABLE = False

    if _TEXTFSM_AVAILABLE:
        try:
            result = _textfsm_parse_profile_table(output)
            if result.success and result.data:
                # Convert from parser dataclass to local dataclass
                return [
                    OltProfileEntry(
                        profile_id=e.profile_id,
                        name=e.name,
                        type=e.type,
                        binding_count=e.binding_count,
                    )
                    for e in result.data
                ]
            if result.warnings:
                logger.debug("TextFSM profile warnings: %s", result.warnings)
        except (ValueError, KeyError, IndexError, AttributeError) as e:
            logger.debug("TextFSM profile parse failed, using legacy: %s", e)

    # Fallback to legacy regex parsing
    return _parse_profile_table_legacy(output, id_col, name_col)


def _parse_tr069_profile_detail_legacy(output: str) -> dict[str, str]:
    """Legacy parser for TR-069 profile detail key-value output.

    Used as fallback when TextFSM parsing fails.
    """
    result: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip().lower()] = value.strip()
    return result


def _parse_tr069_profile_detail(output: str) -> dict[str, str]:
    """Parse ``display ont tr069-server-profile profile-id N`` key-value output.

    Uses TextFSM key-value parser with fallback to legacy regex.
    """
    # Import TextFSM parser if available
    try:
        from app.services.network.parsers import (
            parse_key_value as _textfsm_parse_key_value,
        )

        _TEXTFSM_AVAILABLE = True
    except ImportError:
        _TEXTFSM_AVAILABLE = False

    if _TEXTFSM_AVAILABLE:
        try:
            return _textfsm_parse_key_value(output)
        except (ValueError, KeyError, IndexError, AttributeError) as e:
            logger.debug("TextFSM key-value parse failed, using legacy: %s", e)

    # Fallback to legacy regex parsing
    return _parse_tr069_profile_detail_legacy(output)


def get_line_profiles(olt: OLTDevice) -> tuple[bool, str, list[OltProfileEntry]]:
    """Query OLT for GPON line profiles.

    Returns:
        Tuple of (success, message, list of profiles).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, "display ont-lineprofile gpon all")
        entries = _parse_profile_table(output)
        return True, f"Found {len(entries)} line profile(s)", entries
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error reading line profiles from OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def get_service_profiles(olt: OLTDevice) -> tuple[bool, str, list[OltProfileEntry]]:
    """Query OLT for GPON service profiles.

    Returns:
        Tuple of (success, message, list of profiles).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, "display ont-srvprofile gpon all")
        entries = _parse_profile_table(output)
        return True, f"Found {len(entries)} service profile(s)", entries
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error reading service profiles from OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def get_tr069_server_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[Tr069ServerProfile]]:
    """Query OLT for TR-069 server profiles with per-profile detail.

    Returns:
        Tuple of (success, message, list of Tr069ServerProfile).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []

    try:
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        output = _run_huawei_cmd(channel, "display ont tr069-server-profile all")
        summary = _parse_profile_table(output)

        profiles: list[Tr069ServerProfile] = []
        for entry in summary:
            detail_output = _run_huawei_cmd(
                channel,
                f"display ont tr069-server-profile profile-id {entry.profile_id}",
            )
            detail = _parse_tr069_profile_detail(detail_output)
            profiles.append(
                Tr069ServerProfile(
                    profile_id=entry.profile_id,
                    name=detail.get("profile-name", entry.name),
                    acs_url=detail.get(
                        "url", detail.get("acs url", detail.get("acs-url", ""))
                    ),
                    acs_username=detail.get(
                        "user name", detail.get("acs username", "")
                    ),
                    inform_interval=int(detail.get("inform interval", "0") or "0"),
                    binding_count=int(
                        detail.get("binding times", detail.get("bindnumber", "0"))
                        or "0"
                    ),
                )
            )

        return True, f"Found {len(profiles)} TR-069 server profile(s)", profiles
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error reading TR-069 profiles from OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def ensure_wan_srvprofile(
    olt: OLTDevice,
    *,
    profile_id: int,
    profile_name: str,
    vlan_id: int,
) -> tuple[bool, str]:
    """Ensure the Huawei ONT WAN profile used by ``ont wan-config`` exists.

    The PPPoE VLAN is configured per ONT by ``ont ipconfig ... pppoe vlan``.
    The WAN profile controls routed mode and NAT for the WAN index.
    """
    from app.services.network import olt_ssh as core

    def _run_slow(command: str) -> str:
        from app.services.network.olt_ssh_ont._common import _send_slow

        logger.debug("OLT slow command: %r", command)
        _send_slow(channel, command)
        out = core._read_until_prompt(
            channel,
            rf"{config_prompt}|<cr>|{core._HUAWEI_OPTIONAL_ARG_PROMPT}",
            timeout_sec=12,
        )
        if core._needs_huawei_command_confirm(out):
            channel.send("\n")
            out = core._read_until_prompt(channel, config_prompt, timeout_sec=12)
        return out

    if profile_id < 1:
        return False, "WAN service profile ID must be positive."
    if vlan_id < 1 or vlan_id > 4094:
        return False, "WAN service profile VLAN must be between 1 and 4094."
    if not _TR069_PROFILE_NAME_RE.match(profile_name):
        return (
            False,
            "Invalid profile name (alphanumeric, dashes, dots, spaces, max 64 chars)",
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

        display_cmd = f"display ont wan-profile profile-id {profile_id}"
        current = core._run_huawei_cmd(channel, display_cmd, prompt=config_prompt)
        current_lower = current.lower()
        if not core.is_error_output(current) and (
            str(profile_id) in current_lower or profile_name.lower() in current_lower
        ):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            return True, f"ONT WAN profile {profile_id} already exists."

        create_cmd = f'ont wan-profile profile-id {profile_id}'
        output = _run_slow(create_cmd)
        if core.is_error_output(output):
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            return False, f"OLT rejected ONT WAN profile create: {output.strip()[-200:]}"

        commands = [
            "connection-type route",
            "nat enable",
            "quit",
        ]
        for cmd in commands:
            output = _run_slow(cmd)
            if core.is_error_output(output):
                core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
                return False, f"OLT rejected '{cmd}': {output.strip()[-200:]}"

        verify = core._run_huawei_cmd(channel, display_cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        if core.is_error_output(verify):
            return False, f"ONT WAN profile verification failed: {verify.strip()[-200:]}"
        return True, f"ONT WAN profile {profile_id} ready for PPPoE VLAN {vlan_id}."
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error(
            "Error ensuring WAN service profile on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}"
    finally:
        transport.close()


def create_tr069_server_profile(
    olt: OLTDevice,
    *,
    profile_name: str,
    acs_url: str,
    username: str = "",
    password: str = "",
    inform_interval: int = 300,
) -> tuple[bool, str]:
    """Create a new TR-069 server profile on the OLT via SSH.

    Args:
        olt: The OLT device.
        profile_name: Name for the new profile (alphanumeric, dashes, dots, spaces).
        acs_url: The ACS URL (e.g. http://oss.dotmac.ng:7547).
        username: CWMP ACS username.
        password: CWMP ACS password.
        inform_interval: Periodic inform interval in seconds.

    Returns:
        Tuple of (success, message).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _read_until_prompt,
        _run_huawei_cmd,
    )

    if not _TR069_PROFILE_NAME_RE.match(profile_name):
        return (
            False,
            "Invalid profile name (alphanumeric, dashes, dots, spaces, max 64 chars)",
        )
    if (
        not acs_url
        or "\n" in acs_url
        or "\r" in acs_url
        or ";" in acs_url
        or "|" in acs_url
    ):
        return False, "Invalid ACS URL"
    if username and ("\n" in username or ";" in username or "|" in username):
        return False, "Invalid username"
    if password and ("\n" in password or ";" in password or "|" in password):
        return False, "Invalid password"

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}"

    try:
        import time

        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        # Huawei MA56xx uses an interactive wizard for `add`.
        # Each prompt looks like: { url<K> }: or { user-password<S> }:
        # We extract the prompt text between { } to determine the response.
        # Key: match ONLY the { } prompt content, not echoed output.
        channel.send(f'ont tr069-server-profile add profile-name "{profile_name}"\n')

        for _attempt in range(8):
            time.sleep(2)
            raw = b""
            while channel.recv_ready():
                raw += channel.recv(4096)
                time.sleep(0.1)
            decoded = raw.decode("ascii", errors="replace")

            # Extract the wizard prompt: { ... }:
            prompt_match = re.search(r"\{([^}]+)\}\s*:", decoded)
            if prompt_match:
                prompt_text = prompt_match.group(1).lower().strip()

                if "url" in prompt_text and "user" not in prompt_text:
                    channel.send(f'url "{acs_url}"\n')
                elif "user-password" in prompt_text or "password" in prompt_text:
                    # <S> type prompt — send raw value, no keyword prefix
                    channel.send(f"{password}\n" if password else "\n")
                elif "user" in prompt_text:
                    channel.send(f'user "{username}"\n' if username else "\n")
                elif "interval" in prompt_text or "inform" in prompt_text:
                    channel.send(f"{inform_interval}\n" if inform_interval else "\n")
                elif "<cr>" in prompt_text:
                    channel.send("\n")
                else:
                    channel.send("\n")
            elif re.search(config_prompt, decoded):
                break  # Back at config prompt — wizard complete

        # Drain remaining output
        time.sleep(1)
        while channel.recv_ready():
            channel.recv(4096)

        # Verify profile was created
        verify_output = _run_huawei_cmd(
            channel, "display ont tr069-server-profile all", prompt=config_prompt
        )
        if profile_name.lower() not in verify_output.lower():
            _run_huawei_cmd(channel, "quit", prompt=config_prompt)
            return (
                False,
                f"Profile '{profile_name}' not found after creation. OLT may have rejected it.",
            )

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        logger.info(
            "Created TR-069 profile '%s' on OLT %s with ACS URL %s",
            profile_name,
            olt.name,
            acs_url,
        )
        return True, f"TR-069 profile '{profile_name}' created successfully"
    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        logger.error("Error creating TR-069 profile on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()
