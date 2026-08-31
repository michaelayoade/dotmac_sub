"""Huawei ONT policy-route binding via OLT SSH."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network.olt_ssh_ont._common import (
    _SSH_CONNECTION_ERRORS,
    invalid_fsp_message,
)
from app.services.network.parsers.cli import canonical_fsp

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OntPolicyRouteBinding:
    policy_profile_id: int
    eth_ports: tuple[int, ...]
    ssid1: bool
    readback: str
    port_route_readback: str


def _run_interface_command(channel, command: str, prompt: str) -> str:
    from app.services.network import olt_ssh as core

    channel.send(f"{command}\n")
    return core._read_until_prompt(channel, prompt, timeout_sec=12)


def bind_ont_policy_route(
    olt: OLTDevice,
    fsp: str,
    ont_id: int,
    *,
    policy_profile_id: int = 0,
    eth_ports: tuple[int, ...] = (1, 2, 3, 4),
    ssid1: bool = True,
) -> tuple[bool, str, OntPolicyRouteBinding | None]:
    """Bind a Huawei ONT route-policy profile and enable routed LAN ports.

    Huawei routed/NAT internet created by ``ont ipconfig`` + ``ont wan-config``
    can be absent from ACS as ``WANPPPConnection``. The OLT-side route-policy
    binding is the OMCI equivalent of attaching the internet WAN to customer
    LAN/WLAN interfaces. The policy profile carries WLAN membership; per-port
    ``ont port route`` enables the Ethernet ports.
    """
    from app.services.network import olt_ssh as core

    parts = canonical_fsp(fsp)
    if parts is None:
        return False, invalid_fsp_message(fsp), None
    if not eth_ports and not ssid1:
        return False, "Select at least one LAN port or SSID to bind.", None
    invalid_ports = tuple(port for port in eth_ports if port < 1 or port > 4)
    if invalid_ports:
        return False, f"Invalid ETH route port(s): {invalid_ports}", None

    try:
        transport, channel, _policy = core._open_shell(olt)
    except (*_SSH_CONNECTION_ERRORS, ValueError) as exc:
        return False, f"Connection failed: {exc}", None

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)
        core._run_huawei_cmd(
            channel, f"interface gpon {parts.frame_slot}", prompt=config_prompt
        )

        apply_outputs: list[str] = []
        route_cmd = (
            f"ont policy-route-config {parts.port} {ont_id} "
            f"profile-id {policy_profile_id}"
        )
        apply_outputs.append(_run_interface_command(channel, route_cmd, config_prompt))
        for eth_port in eth_ports:
            apply_outputs.append(
                _run_interface_command(
                    channel,
                    f"ont port route {parts.port} {ont_id} eth {eth_port} enable",
                    config_prompt,
                )
            )

        failures = [
            output.strip()
            for output in apply_outputs
            if core.is_error_output(output)
            and "make configuration repeatedly" not in output.lower()
        ]
        if failures:
            message = "; ".join(failure[-160:] for failure in failures)
            return False, f"Huawei policy-route bind failed: {message}", None

        readback = core._run_huawei_cmd(
            channel,
            f"display ont routing-table policy-route {parts.port} {ont_id}",
            prompt=config_prompt,
        )
        port_readback = core._run_huawei_cmd(
            channel,
            f"display ont port route {parts.port} {ont_id} eth all",
            prompt=config_prompt,
        )
        if core.is_error_output(readback):
            return (
                False,
                f"Huawei policy-route readback failed: {readback.strip()[-160:]}",
                None,
            )

        expected_ports = tuple(dict.fromkeys(eth_ports))
        missing_ports = [
            port
            for port in expected_ports
            if not re.search(rf"\bETH\s+{port}\s+enable\b", port_readback)
        ]
        if missing_ports:
            return (
                False,
                f"ETH route enable readback missing port(s): {missing_ports}",
                None,
            )
        if ssid1 and "SSID1" not in readback:
            return False, "Policy-route readback does not include SSID1.", None

        binding = OntPolicyRouteBinding(
            policy_profile_id=policy_profile_id,
            eth_ports=expected_ports,
            ssid1=ssid1,
            readback=readback,
            port_route_readback=port_readback,
        )
        return (
            True,
            "Huawei policy-route bound to "
            + ", ".join(
                [
                    *(f"LAN{port}" for port in expected_ports),
                    *(["SSID1"] if ssid1 else []),
                ]
            ),
            binding,
        )
    except (*_SSH_CONNECTION_ERRORS, RuntimeError, SSHException) as exc:
        logger.error(
            "Error binding Huawei policy-route on OLT %s: %s",
            olt.name,
            exc,
            exc_info=True,
        )
        return False, f"Error: {exc}", None
    finally:
        transport.close()
