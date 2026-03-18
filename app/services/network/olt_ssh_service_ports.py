"""Focused OLT SSH actions for service-port operations."""

from __future__ import annotations

import logging

from app.models.network import OLTDevice
from app.services.network.olt_ssh import ServicePortEntry

logger = logging.getLogger(__name__)


def get_service_ports_for_ont(
    olt: OLTDevice, fsp: str, ont_id: int
) -> tuple[bool, str, list[ServicePortEntry]]:
    """Return only the service-ports belonging to a specific ONT."""
    from app.services.network import olt_ssh as core

    ok, msg, all_ports = core.get_service_ports(olt, fsp)
    if not ok:
        return False, msg, []
    filtered = [p for p in all_ports if p.ont_id == ont_id]
    return True, f"Found {len(filtered)} service-port(s) for ONT {ont_id}", filtered


def delete_service_port(olt: OLTDevice, index: int) -> tuple[bool, str]:
    """Delete a service-port from the OLT by index."""
    from app.services.network import olt_ssh as core

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
        output = core._run_huawei_cmd(channel, f"undo service-port {index}", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning("Failed to delete service-port %d on OLT %s: %s", index, olt.name, output.strip()[-150:])
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Deleted service-port %d on OLT %s", index, olt.name)
        return True, f"Service-port {index} deleted"
    except Exception as exc:
        logger.error("Error deleting service-port on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()


def create_single_service_port(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    *,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
) -> tuple[bool, str]:
    """Create a single service-port on an OLT."""
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err

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
        cmd = core.build_service_port_command(
            fsp=fsp,
            ont_id=ont_id,
            gem_index=gem_index,
            vlan_id=vlan_id,
            user_vlan=user_vlan,
            tag_transform=tag_transform,
        )
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning("Service-port creation failed on OLT %s: %s", olt.name, output.strip()[-150:])
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info(
            "Created service-port VLAN %d GEM %d for ONT %d on OLT %s %s",
            vlan_id,
            gem_index,
            ont_id,
            olt.name,
            fsp,
        )
        return True, f"Service-port created (VLAN {vlan_id}, GEM {gem_index})"
    except Exception as exc:
        logger.error("Error creating service-port on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}"
    finally:
        transport.close()
