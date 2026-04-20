"""Focused OLT SSH actions for service-port operations."""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services.network.olt_inventory import get_olt_or_none
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


def get_service_port_by_index(
    olt: OLTDevice, index: int
) -> tuple[bool, str, ServicePortEntry | None]:
    """Read one service-port by global OLT index."""
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        output = core._run_huawei_paged_cmd(channel, f"display service-port {index}")
        if core.is_error_output(output):
            return False, f"OLT rejected: {output.strip()[-150:]}", None
        entries = core._parse_service_port_table(output)
        for entry in entries:
            if entry.index == index:
                return True, f"Found service-port {index}", entry
        return True, f"Service-port {index} was not found", None
    except Exception as exc:
        logger.error(
            "Error reading service-port %d on OLT %s: %s", index, olt.name, exc
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()


def clone_service_ports(
    db: Session, olt_id: str, fsp: str, ont_id: int
) -> tuple[bool, str]:
    """Clone service-ports for an ONT using a reference ONT on the same port."""
    from app.services.network import olt_ssh as core

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"

    ok, msg, entries = core.get_service_ports(olt, fsp)
    if not ok or not entries:
        return False, f"Cannot read service-ports: {msg}"

    ont_counts = Counter(e.ont_id for e in entries)
    if not ont_counts:
        return False, "No existing service-ports to learn from"

    new_ont_ports = [e for e in entries if e.ont_id == ont_id]
    if new_ont_ports:
        return True, f"ONT {ont_id} already has {len(new_ont_ports)} service-port(s)"

    reference_ont_id = None
    for candidate_ont_id, _count in ont_counts.most_common():
        if candidate_ont_id != ont_id:
            reference_ont_id = candidate_ont_id
            break
    if reference_ont_id is None:
        return False, "No reference ONT found to learn service-port pattern from"

    reference_ports = [e for e in entries if e.ont_id == reference_ont_id]
    logger.info(
        "Learning service-port pattern from ONT %d (%d ports) for new ONT %d on %s",
        reference_ont_id,
        len(reference_ports),
        ont_id,
        fsp,
    )

    return core.create_service_ports(olt, fsp, ont_id, reference_ports)


def provision_ont_service_ports(
    db: Session, olt_id: str, fsp: str, ont_id: int
) -> tuple[bool, str]:
    """Compatibility alias for explicit service-port cloning."""
    return clone_service_ports(db, olt_id, fsp, ont_id)


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
        output = core._run_huawei_cmd(
            channel, f"undo service-port {index}", prompt=config_prompt
        )
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Failed to delete service-port %d on OLT %s: %s",
                index,
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}"

        logger.info("Deleted service-port %d on OLT %s", index, olt.name)
        core._invalidate_olt_read_cache(olt, "service_ports", "running_config")
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
    port_index: int | None = None,
) -> tuple[bool, str, int | None]:
    """Create a single service-port on an OLT.

    Args:
        olt: OLT device to connect to.
        fsp: Frame/Slot/Port string.
        ont_id: ONT ID on the PON port.
        gem_index: GEM port index.
        vlan_id: Service VLAN ID.
        user_vlan: User VLAN (default: same as vlan_id).
        tag_transform: VLAN tag transform mode.
        port_index: Pre-allocated service-port index. If None, OLT auto-assigns.

    Returns:
        (success, message, assigned_index). assigned_index is the port_index used,
        or None if not available.
    """
    from app.services.network import olt_ssh as core

    ok, err = core._validate_fsp(fsp)
    if not ok:
        return False, err, None

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (core.SSHException, OSError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None
    except Exception as exc:
        logger.error("Error connecting to OLT %s: %s", olt.name, exc)
        return False, f"Unexpected error: {type(exc).__name__}", None

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
            port_index=port_index,
        )
        output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if core.is_error_output(output):
            logger.warning(
                "Service-port creation failed on OLT %s: %s",
                olt.name,
                output.strip()[-150:],
            )
            return False, f"OLT rejected: {output.strip()[-150:]}", None

        logger.info(
            "Created service-port %s VLAN %d GEM %d for ONT %d on OLT %s %s",
            port_index or "auto",
            vlan_id,
            gem_index,
            ont_id,
            olt.name,
            fsp,
        )
        core._invalidate_olt_read_cache(olt, "service_ports", "running_config")
        return True, f"Service-port created (VLAN {vlan_id}, GEM {gem_index})", port_index
    except Exception as exc:
        logger.error("Error creating service-port on OLT %s: %s", olt.name, exc)
        return False, f"Error: {exc}", None
    finally:
        transport.close()
