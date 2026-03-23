"""Focused OLT SSH actions for profile inspection."""

from __future__ import annotations

import logging

from app.models.network import OLTDevice
from app.services.network.olt_ssh import OltProfileEntry, Tr069ServerProfile

logger = logging.getLogger(__name__)


def get_line_profiles(olt: OLTDevice) -> tuple[bool, str, list[OltProfileEntry]]:
    """Fetch line profiles from a Huawei OLT."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        output = core._run_huawei_cmd(channel, "display ont-lineprofile gpon all")
        return True, "Line profiles loaded", core._parse_profile_table(output)
    except Exception as exc:
        logger.error("Error reading line profiles from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def get_service_profiles(olt: OLTDevice) -> tuple[bool, str, list[OltProfileEntry]]:
    """Fetch service profiles from a Huawei OLT."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        output = core._run_huawei_cmd(channel, "display ont-srvprofile gpon all")
        return True, "Service profiles loaded", core._parse_profile_table(output)
    except Exception as exc:
        logger.error("Error reading service profiles from OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", []
    finally:
        transport.close()


def get_tr069_server_profiles(
    olt: OLTDevice,
) -> tuple[bool, str, list[Tr069ServerProfile]]:
    """Fetch TR-069 server profiles from a Huawei OLT."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", []
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", []

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        output = core._run_huawei_cmd(channel, "display ont tr069-server-profile all")
        profiles: list[Tr069ServerProfile] = []
        for entry in core._parse_profile_table(output):
            detail_out = core._run_huawei_cmd(
                channel,
                f"display ont tr069-server-profile profile-id {entry.profile_id}",
            )
            detail = core._parse_tr069_profile_detail(detail_out)
            profiles.append(
                core.Tr069ServerProfile(
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
        logger.error(
            "Error reading TR-069 server profiles from OLT %s: %s", olt.name, exc
        )
        return False, f"Error: {exc}", []
    finally:
        transport.close()
