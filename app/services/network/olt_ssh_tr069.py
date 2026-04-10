"""OLT SSH actions for TR-069 server profile management."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tr069ServerProfile:
    """A TR-069 server profile with detail fields."""

    profile_id: int
    name: str
    acs_url: str = ""
    acs_username: str = ""
    inform_interval: int = 0
    binding_count: int = 0


_TR069_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,64}$")


def _parse_tr069_profile_detail(output: str) -> dict[str, str]:
    """Parse ``display ont tr069-server-profile profile-id N`` key-value output."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip().lower()] = value.strip()
    return result


def get_tr069_server_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[Tr069ServerProfile]]:
    """Query OLT for TR-069 server profiles with per-profile detail.

    Returns:
        Tuple of (success, message, list of Tr069ServerProfile).
    """
    from app.services.network.olt_ssh import (
        _open_shell,
        _parse_profile_table,
        _read_until_prompt,
        _run_huawei_cmd,
    )

    try:
        transport, channel, policy = _open_shell(olt)
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
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
    except Exception as exc:
        logger.error("Error reading TR-069 profiles from OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}", []
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
    except (SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}"
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Unexpected error: {type(exc).__name__}"

    try:
        import time

        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        _run_huawei_cmd(channel, "config", prompt=config_prompt)

        # Huawei MA56xx uses an interactive wizard for `add`.
        # Each prompt looks like: { url<K> }: or { user-password<S> }:
        # We extract the prompt text between { } to determine the response.
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
                f"Profile '{profile_name}' not found after creation. "
                "OLT may have rejected it.",
            )

        _run_huawei_cmd(channel, "quit", prompt=config_prompt)

        logger.info(
            "Created TR-069 profile '%s' on OLT %s with ACS URL %s",
            profile_name,
            olt.name,
            acs_url,
        )
        return True, f"TR-069 profile '{profile_name}' created successfully"
    except Exception as exc:
        logger.error("Error creating TR-069 profile on OLT %s: %s", olt.name, exc, exc_info=True)
        return False, f"Error: {exc}"
    finally:
        transport.close()
